import json
from app.logger import get_logger, setup_logging
from pathlib import Path

from app.detectors.event_detector import DetectionEvent
from app.emergency.models import (
    Instruction,
    RiskAssessment,
    RiskLevel,
    TriggerConditions,
)
from app.location.models import RoadContext

setup_logging()
logger = get_logger(__name__)

DEFAULT_INSTRUCTIONS_FILE = Path(__file__).parent / "data" / "instructions.json"

# وزن اختصاصی‌بودن شرط‌ها. دستورالعملی که شرط‌های بیشتری را برآورده
# می‌کند، بر دستورالعمل عمومی‌تر مقدم است.
W_PARKING = 20
W_WINDING = 12
W_ROAD_TYPES = 10
W_MIN_RISK = 5

# آخرین راه‌حل: فقط وقتی هیچ دستورالعملی پیدا نشود (مثلاً فایل ناقص).
LAST_RESORT = Instruction(
    id="last_resort",
    title="راهنمای عمومی ایمنی",
    headline="فلاشر روشن، ترمز ناگهانی نکنید، در معرض ترافیک نباشید",
    trigger_conditions=TriggerConditions(event_type="*"),
    risk_level=RiskLevel.UNKNOWN,
    steps=[
        "فلاشر را روشن کنید.",
        "از ترمز یا تغییر مسیر ناگهانی خودداری کنید.",
        "اگر ایمن است، خودرو را از مسیر عبور خارج کنید.",
        "اگر خروج از خودرو ایمن نیست، با کمربند داخل آن بمانید.",
        "در صورت نیاز با امداد تماس بگیرید.",
    ],
    warnings=["در معرض ترافیک تعمیر نکنید."],
    status="draft",
)


def load_instructions(path: Path | None = None) -> list[Instruction]:
    """فایل JSON را می‌خواند و اعتبارسنجی می‌کند. در صورت خطا استثنا می‌دهد."""
    path = path or DEFAULT_INSTRUCTIONS_FILE
    with path.open("r", encoding="utf-8") as file:
        raw = json.load(file)

    items = [Instruction.model_validate(item) for item in raw]

    ids = [item.id for item in items]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"Duplicate instruction ids: {sorted(duplicates)}")
    return items


class RecommendationEngine:
    """
    انتخاب دستورالعمل مناسب بر اساس رویداد، ارزیابی خطر و اطلاعات جاده.

    قواعد:
    - ارزیابی UNKNOWN فقط دستورالعمل‌های با risk_level=UNKNOWN را می‌گیرد.
    - دستورالعمل UNKNOWN هیچ‌وقت برای ارزیابی معلوم انتخاب نمی‌شود.
    - شرطی که روی اطلاعات نامعلوم (None) تکیه کند، برقرار نیست.
    - در تساوی، ابتدا امتیاز اختصاصی‌بودن و بعد priority تعیین می‌کند.
    """

    def __init__(self, instructions: list[Instruction] | None = None):
        self.instructions = (
            instructions if instructions is not None else load_instructions()
        )

    def select(
        self,
        event: DetectionEvent,
        assessment: RiskAssessment,
        road: RoadContext | None,
    ) -> Instruction:
        best: Instruction | None = None
        best_key: tuple[int, int] | None = None

        for instruction in self.instructions:
            score = self._match_score(instruction, event, assessment, road)
            if score is None:
                continue
            key = (score, instruction.priority)
            if best_key is None or key > best_key:
                best, best_key = instruction, key

        if best is None:
            logger.warning(
                "No instruction matched event=%s level=%s; using last resort.",
                event.event_type,
                assessment.level.value,
            )
            return LAST_RESORT
        return best

    @staticmethod
    def _match_score(
        instruction: Instruction,
        event: DetectionEvent,
        assessment: RiskAssessment,
        road: RoadContext | None,
    ) -> int | None:
        """None یعنی تطابق ندارد؛ در غیر این‌صورت امتیاز اختصاصی‌بودن."""
        cond = instruction.trigger_conditions
        level = assessment.level
        score = 0

        if cond.event_type != event.event_type:
            return None

        # جداسازی کامل شاخه‌ی UNKNOWN
        if level == RiskLevel.UNKNOWN:
            return 0 if instruction.risk_level == RiskLevel.UNKNOWN else None
        if instruction.risk_level == RiskLevel.UNKNOWN:
            return None

        if cond.min_risk is not None:
            if level.rank < cond.min_risk.rank:
                return None
            score += W_MIN_RISK

        if cond.road_types is not None:
            if road is None or road.highway_type not in cond.road_types:
                return None
            score += W_ROAD_TYPES

        if cond.is_winding is not None:
            if road is None or road.is_winding != cond.is_winding:
                return None
            score += W_WINDING

        if cond.is_parking_area is not None:
            if road is None or road.is_parking_area != cond.is_parking_area:
                return None
            score += W_PARKING

        return score