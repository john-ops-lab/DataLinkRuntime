import math

def handle(context, input):
    """
    处理输入中的数据行，逐行校验并汇总结果。

    输入约定：
      - input 为包含 "rows" 键的对象，rows 为对象数组
      - 每行必须包含 id（字符串，去空白后非空）与 amount（数字或数字字符串）

    返回值（对象，字段名固定）：
      - acceptedCount：通过校验的行数
      - invalidCount：未通过校验的行数
      - total：所有通过校验的 amount 之和

    行为约束：
      - 布尔型 amount 视为非法（即使其数值看似合法）
      - amount 必须为有限且 >= 0 的数字，或可解析为有限非负数的非空数字字符串
      - 单行非法不会中断整体流程，仅按无效计 1
      - rows 缺失或非数组时，三项返回值均为 0
      - 仅使用 Python 标准库，不访问凭据、网络或文件系统
    """
    # 默认返回零值结果，用于 rows 缺失或类型不符的场景
    zero_result = {"acceptedCount": 0, "invalidCount": 0, "total": 0}

    # 防御性判断：input 非字典或 rows 非列表时直接返回全 0
    if not isinstance(input, dict):
        return zero_result
    rows = input.get("rows")
    if not isinstance(rows, list):
        return zero_result

    # 累计计数器与合计金额
    accepted_count = 0
    invalid_count = 0
    total = 0

    for row in rows:
        # 非字典行按无效处理，继续下一行
        if not isinstance(row, dict):
            invalid_count += 1
            continue

        # 校验 id：必须为字符串，且去除首尾空白后非空
        raw_id = row.get("id")
        if not isinstance(raw_id, str):
            invalid_count += 1
            continue
        if not raw_id.strip():
            invalid_count += 1
            continue

        # 校验 amount：先排除布尔型，再判断数字或可解析的非空数字字符串
        raw_amount = row.get("amount")
        # 显式将 bool 排除，避免 True/False 通过数值校验
        if isinstance(raw_amount, bool):
            invalid_count += 1
            continue

        amount_value = None
        if isinstance(raw_amount, (int, float)):
            # 数字类型（int/float，bool 已在前面排除）：使用 math.isfinite 同时排除 NaN 与正负无穷，
            # 再要求 >= 0，确保仅接受有限非负数。
            if math.isfinite(raw_amount) and raw_amount >= 0:
                amount_value = raw_amount
        elif isinstance(raw_amount, str):
            # 字符串类型：去空白后非空，且可解析为有限非负数
            stripped = raw_amount.strip()
            if stripped:
                try:
                    parsed = float(stripped)
                except (ValueError, OverflowError):
                    parsed = None
                # 解析结果需为有限非负数：math.isfinite 同时排除 NaN 与正负无穷，
                # 再叠加 >= 0 校验。
                if parsed is not None and math.isfinite(parsed) and parsed >= 0:
                    amount_value = parsed

        # 根据校验结果更新计数器与合计
        if amount_value is not None:
            accepted_count += 1
            total += amount_value
        else:
            invalid_count += 1

    # 返回固定字段名的对象
    return {"acceptedCount": accepted_count, "invalidCount": invalid_count, "total": total}
