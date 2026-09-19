"""LLM 价目表加载与成本折算（M-12，docs/01 §5.10 成本看板数据源）。

价目放 config/pricing.yaml（模型 → 每 1K token 单价 USD）。与预算配置
（app/context/budget.py）同款目录探测：Settings 显式指定 → CWD → 仓库根
上溯；区别在于缺失/未收录**不 fail fast**——定价只是看板估算，成本记 0
并记 warning，绝不阻断用量落库。
"""

import logging
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import yaml

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# 成本列精度（token_usages.cost_usd 为 Numeric(10,6)）
_QUANT = Decimal("0.000001")


@lru_cache(maxsize=1)
def _load_table() -> dict[str, tuple[Decimal, Decimal]]:
    """加载价目表（进程内一份）。

    Returns:
        {模型名: (输入单价, 输出单价)}（USD / 1K tokens）；
        文件缺失/结构非法时返回空表（warning 提示，不抛异常）。
    """
    candidates: list[Path] = []
    configured = get_settings().pricing_config_dir
    if configured:
        candidates.append(Path(configured))
    candidates.append(Path.cwd() / "config")
    # 本文件位于 <repo>/services/memory-service/app/observability/pricing.py，
    # 连续 5 次 parent 上溯到仓库根：observability → app → memory-service →
    # services → <repo>（与 budget.py 同款；容器内源码平铺于 /app 时不命中，
    # 由 Settings/CWD 候选兜底）
    repo_root = Path(__file__).resolve()
    for _ in range(5):
        repo_root = repo_root.parent
    candidates.append(repo_root / "config")

    path = next((p / "pricing.yaml" for p in candidates if (p / "pricing.yaml").is_file()), None)
    if path is None:
        logger.warning("pricing_config_missing cost_usd 将恒为 0（已尝试 %s）", candidates)
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        table = {
            str(model): (
                Decimal(str(prices["input_per_1k_usd"])),
                Decimal(str(prices["output_per_1k_usd"])),
            )
            for model, prices in (raw.get("models") or {}).items()
        }
        logger.info("pricing_loaded entries=%s source=%s", len(table), path)
        return table
    except Exception as exc:  # 非法 YAML/缺字段：降级空表，不阻断业务
        logger.warning("pricing_config_invalid error=%s", exc)
        return {}


def cost_usd_for(model: str | None, prompt_tokens: int, completion_tokens: int) -> Decimal:
    """按价目表折算一轮 LLM 成本（USD，6 位小数）。

    Args:
        model: 模型名（Settings.llm_model 等）；None/未收录返回 0。
        prompt_tokens: 输入 token 数。
        completion_tokens: 输出 token 数。

    Returns:
        折算成本；模型未收录时 Decimal("0")。
    """
    if not model:
        return Decimal("0")
    entry = _load_table().get(model)
    if entry is None:
        return Decimal("0")
    input_usd, output_usd = entry
    cost = (Decimal(prompt_tokens) * input_usd + Decimal(completion_tokens) * output_usd) / Decimal(
        1000
    )
    return cost.quantize(_QUANT)
