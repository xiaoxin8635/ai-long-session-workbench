"""上下文预算 profile 加载（M-05，config/budgets/*.yaml）。

YAML 写占比（分母 = context_window_tokens − reserve_output），
加载时换算为各区块绝对 token 数——窗口调整时 YAML 无需修改。

目录解析顺序（fail fast，找不到即报错而非静默兜底）：
  1. Settings.budget_config_dir（环境变量显式指定）
  2. <CWD>/config/budgets（容器内挂载于 /app/config）
  3. <仓库根>/config/budgets（本文件上溯五级，本地开发与 pytest）
"""

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from app.context.schemas import SectionKey
from app.context.tokenizer import count_tokens
from app.core.config import get_settings

logger = logging.getLogger(__name__)

# 全部区块（YAML 必须完整给出，缺失即配置错误）
_ALL_SECTIONS = tuple(SectionKey)


class BudgetConfigError(Exception):
    """预算配置缺失或非法（部署错误，启动/调用时应立即暴露）。"""


@dataclass(frozen=True)
class ContextBudget:
    """一次装配的绝对 token 预算（docs/06 §7 核心接口）。

    Attributes:
        window_tokens: 总窗口（context_window_tokens）。
        reserve_output: 输出预留（window 的一部分，不参与区块分配）。
        sections: 各区块绝对预算（token 数）；system 为 -1 表示不裁剪。
        evict_order: 全局超预算时从前往后整区块放弃的顺序（不含 system）。
        profile: 来源 profile 名（统计与日志用）。
    """

    window_tokens: int
    reserve_output: int
    sections: dict[SectionKey, int]
    evict_order: tuple[SectionKey, ...]
    profile: str

    @property
    def available_tokens(self) -> int:
        """区块可用总量（窗口减输出预留，全局防线阈值）。"""
        return self.window_tokens - self.reserve_output


def resolve_budget_dir() -> Path:
    """解析预算 YAML 目录（Settings 显式配置 → CWD → 仓库根探测）。

    Returns:
        存在的预算目录路径。

    Raises:
        BudgetConfigError: 三处均未找到。
    """
    candidates: list[Path] = []
    configured = get_settings().budget_config_dir
    if configured:
        candidates.append(Path(configured))
    candidates.append(Path.cwd() / "config" / "budgets")
    # 本文件位于 <repo>/services/memory-service/app/context/budget.py，
    # 连续 5 次 parent 上溯到仓库根：context → app → memory-service →
    # services → <repo>（Path 到根后 parent 自返回根，不越界；容器内源码
    # 平铺于 /app 时该候选不命中，由 Settings/CWD 候选兜底）
    repo_root = Path(__file__).resolve()
    for _ in range(5):
        repo_root = repo_root.parent
    candidates.append(repo_root / "config" / "budgets")
    for path in candidates:
        if path.is_dir():
            return path
    raise BudgetConfigError(
        f"预算配置目录不存在，已尝试: {[str(p) for p in candidates]}"
        "（容器内请挂载 config/ 到 /app/config，或设置 MEMORY_SERVICE_BUDGET_CONFIG_DIR）"
    )


def _validate(raw: dict, profile: str) -> tuple[float, dict[SectionKey, float], list[str]]:
    """校验 YAML 结构与数值约束。

    Args:
        raw: yaml.safe_load 的结果。
        profile: profile 名（错误信息用）。

    Returns:
        (reserve 占比, 各区块占比, evict_order 字符串列表)。

    Raises:
        BudgetConfigError: 结构缺失 / 占比越界 / 区块不完整 / 总和超 1。
    """
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise BudgetConfigError(f"profile={profile} 缺少 version: 1 或结构非法")
    reserve = raw.get("reserve_output")
    if not isinstance(reserve, int | float) or not 0.0 < float(reserve) < 1.0:
        raise BudgetConfigError(f"profile={profile} reserve_output 必须为 (0,1) 占比")
    sections_raw = raw.get("sections")
    if not isinstance(sections_raw, dict):
        raise BudgetConfigError(f"profile={profile} 缺少 sections 映射")
    try:
        fractions = {SectionKey(k): float(v) for k, v in sections_raw.items()}
    except ValueError as exc:
        raise BudgetConfigError(f"profile={profile} sections 含未知区块名: {exc}") from exc
    missing = [s.value for s in _ALL_SECTIONS if s not in fractions]
    if missing:
        raise BudgetConfigError(f"profile={profile} sections 缺少区块: {missing}")
    for key, value in fractions.items():
        if not 0.0 <= value <= 1.0:
            raise BudgetConfigError(f"profile={profile} 区块 {key.value} 占比越界: {value}")
    if sum(fractions.values()) > 1.0 + 1e-9:
        raise BudgetConfigError(
            f"profile={profile} sections 占比总和 {sum(fractions.values()):.2f} 超过 1.0"
        )
    evict = raw.get("evict_order")
    if not isinstance(evict, list) or not evict:
        raise BudgetConfigError(f"profile={profile} 缺少 evict_order 列表")
    return float(reserve), fractions, [str(e) for e in evict]


def load_budget(profile: str | None = None) -> ContextBudget:
    """加载并换算一个预算 profile。

    Args:
        profile: profile 名；None 时取 Settings.context_budget_profile（default）。

    Returns:
        绝对 token 预算。

    Raises:
        BudgetConfigError: 目录缺失 / YAML 不存在 / 内容非法。
    """
    return _load_budget_cached(profile or get_settings().context_budget_profile)


@lru_cache(maxsize=8)
def _load_budget_cached(name: str) -> ContextBudget:
    """按 profile 名缓存的实际加载实现（YAML 变更需重启进程生效）。"""
    path = resolve_budget_dir() / f"{name}.yaml"
    if not path.is_file():
        raise BudgetConfigError(f"预算 profile 不存在: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise BudgetConfigError(f"profile={name} YAML 解析失败: {exc}") from exc

    reserve_frac, fractions, evict_names = _validate(raw, name)
    window = get_settings().context_window_tokens
    available = int(window * (1.0 - reserve_frac))
    sections: dict[SectionKey, int] = {}
    for key in _ALL_SECTIONS:
        # system 固定注入不裁剪，预算标记 -1（仍计入占用统计）
        if key is SectionKey.SYSTEM:
            sections[key] = -1
        else:
            sections[key] = round(available * fractions[key])
    try:
        evict_order = tuple(SectionKey(n) for n in evict_names)
    except ValueError as exc:
        raise BudgetConfigError(f"profile={name} evict_order 含未知区块: {exc}") from exc
    invalid = [k for k in evict_order if k is SectionKey.SYSTEM]
    if invalid or len(set(evict_order)) != len(evict_order):
        raise BudgetConfigError(f"profile={name} evict_order 含 system/重复项: {evict_names}")

    budget = ContextBudget(
        window_tokens=window,
        reserve_output=window - available,
        sections=sections,
        evict_order=evict_order,
        profile=name,
    )
    logger.debug(
        "budget_loaded profile=%s window=%s available=%s", name, window, budget.available_tokens
    )
    return budget


def system_prompt_tokens(prompt: str) -> int:
    """system 提示的 token 占用（预算统计用；system 不裁剪但需计入总量）。"""
    return count_tokens(prompt)
