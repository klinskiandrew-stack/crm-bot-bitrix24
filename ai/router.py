import structlog
from db.repositories import settings as settings_repo

logger = structlog.get_logger()


class ModelRouter:
    """Route requests to appropriate Claude model."""

    SIMPLE_KEYWORDS = [
        "сколько", "какой статус", "найди", "покажи", "когда",
        "список", "count", "what's", "show", "find", "list"
    ]

    ANALYTICS_KEYWORDS = [
        "проанализируй", "сравни", "динамика", "почему", "как изменилось",
        "интерпретируй", "причины", "analyse", "analyze", "compare", "why", "trend"
    ]

    COMPLEX_KEYWORDS = [
        "системные причины", "стратегические рекомендации", "комплексный анализ",
        "прогноз", "многофакторный", "strategic", "complex", "forecast", "system"
    ]

    def __init__(self):
        self.default_model = "claude-sonnet-4-6"
        self.routing_mode = "auto"  # auto | forced
        self.forced_model = None

    async def init(self):
        """Initialize router settings from database."""
        routing_mode = await settings_repo.get("routing_mode")
        if routing_mode:
            self.routing_mode = routing_mode

        default_model = await settings_repo.get("default_model")
        if default_model:
            self.default_model = default_model

        if self.routing_mode == "forced":
            forced = await settings_repo.get("forced_model")
            if forced:
                self.forced_model = forced

    async def route(self, question: str) -> str:
        """Route question to appropriate model."""
        await self.init()

        # If forced mode, use that model
        if self.routing_mode == "forced" and self.forced_model:
            logger.info("Using forced model", model=self.forced_model)
            return self.forced_model

        # Auto mode: categorize question and select model
        category = self._categorize_question(question)

        model_map = {
            "simple": "claude-sonnet-4-6",
            "analytics": "claude-sonnet-4-6",
            "complex": "claude-opus-4-7"
        }

        selected_model = model_map.get(category, self.default_model)
        logger.info(
            "Auto-routing selected model",
            category=category,
            model=selected_model,
            question_preview=question[:50]
        )
        return selected_model

    async def set_routing_mode(self, mode: str, forced_model: str = None):
        """Set routing mode: auto or forced."""
        self.routing_mode = mode
        await settings_repo.set("routing_mode", mode)

        if mode == "forced" and forced_model:
            self.forced_model = forced_model
            await settings_repo.set("forced_model", forced_model)

        logger.info("Routing mode updated", mode=mode, forced_model=forced_model)

    async def set_default_model(self, model: str):
        """Set default model."""
        self.default_model = model
        await settings_repo.set("default_model", model)
        logger.info("Default model updated", model=model)

    def _categorize_question(self, question: str) -> str:
        """Categorize question by keywords."""
        question_lower = question.lower()

        for keyword in self.COMPLEX_KEYWORDS:
            if keyword in question_lower:
                return "complex"

        for keyword in self.ANALYTICS_KEYWORDS:
            if keyword in question_lower:
                return "analytics"

        for keyword in self.SIMPLE_KEYWORDS:
            if keyword in question_lower:
                return "simple"

        return "analytics"  # default


# Global router instance
router = ModelRouter()
