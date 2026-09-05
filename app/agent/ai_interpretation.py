"""
AI 解读层 —— 云端大模型 Agent
对应时序图第 ④ 阶段：report_context → ai_interpretation
输出结构化解读：结论 + 依据 + 今晚建议 + 安全预设候选
"""
import json
import asyncio
import httpx
from app import config


def build_report_context(summary: dict, weekly_stats: dict, feedback: list = None) -> dict:
    """
    组装 report_context_v1 —— 给大模型的输入
    只传结构化摘要，不传原始音频
    """
    return {
        "version": "v1",
        "daily_summary": {
            "date": summary.get("date", ""),
            "event_count": summary.get("event_count", 0),
            "has_data": summary.get("event_count", 0) > 0,
            "total_rounds": summary.get("total_rounds", 0),
            "success_rounds": summary.get("success_rounds", 0),
            "success_rate": (
                round(summary.get("success_rounds", 0) / summary["total_rounds"], 3)
                if summary.get("total_rounds") else 0
            ),
            "avg_response_time": summary.get("avg_response_time", 0),
            "peak_hour": summary.get("peak_hour", ""),
            "max_vibration_level": summary.get("max_vibration_level", 0),
        },
        "weekly_trend": {
            "nights": weekly_stats.get("nights", 0),
            "avg_rounds_per_night": weekly_stats.get("avg_rounds_per_night", 0),
            "success_rate": weekly_stats.get("success_rate", 0),
            "trend": weekly_stats.get("trend", ""),
        },
        "recent_feedback": feedback or [],
        "safety_boundary": {
            "max_rounds_per_night": 15,
            "max_vibration_seconds": 300,
            "note": "所有建议不能超过这个边界",
        }
    }


def _parse_ai_response(text: str) -> dict:
    """解析大模型返回的 JSON（容错处理）"""
    # 尝试直接解析
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass

    # 尝试提取 ```json ... ``` 块
    import re
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except (json.JSONDecodeError, TypeError):
            pass

    # 尝试从文本中提取第一个平衡的 {...} JSON 对象
    # （代码类模型常在 JSON 前后夹带推理文字）
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        parsed = json.loads(candidate)
                        if isinstance(parsed, dict) and ("summary" in parsed or "trend_label" in parsed):
                            return parsed
                    except (json.JSONDecodeError, TypeError):
                        pass
                    break
        start = text.find("{", start + 1)

    # 全部失败 → 规则版兜底（不再把原始推理文本当 summary 展示给用户）
    fallback = _rule_based_interpretation({})
    fallback["source"] = "rule_based_parse_fail"
    return fallback


def claims_no_record(text: str) -> bool:
    value = str(text or "").lower()
    markers = (
        "暂无有效",
        "暂无记录",
        "尚未收到",
        "没有收到",
        "没有有效",
        "无有效记录",
        "no data",
        "no valid",
        "no recorded",
        "nothing recorded",
        "no events",
    )
    return any(marker in value for marker in markers)


def ground_ai_interpretation(result: dict, report_context: dict) -> dict:
    """Keep model prose consistent with authoritative event counts and metrics."""
    fallback = _rule_based_interpretation(report_context)
    if not isinstance(result, dict):
        result = {}
    grounded = dict(fallback)
    grounded.update(result)
    daily = report_context.get("daily_summary", {})
    has_data = int(daily.get("event_count") or 0) > 0
    summary = str(result.get("summary") or "").strip()
    basis = result.get("basis")
    if not isinstance(basis, list):
        basis = []
    basis = [str(item).strip() for item in basis if str(item).strip()][:3]
    suggestion = str(result.get("tonight_suggestion") or "").strip()
    trend = result.get("trend_label")
    conflict = (claims_no_record(summary) and has_data) or (not has_data and not claims_no_record(summary))
    if conflict or not summary or not basis or not suggestion or trend not in {"improving", "stable", "worsening", "insufficient_data"}:
        grounded.update({
            "summary": fallback["summary"],
            "basis": fallback["basis"],
            "tonight_suggestion": fallback["tonight_suggestion"],
            "trend_label": fallback["trend_label"],
            "grounded_by_rules": True,
        })
    else:
        grounded.update({
            "summary": summary,
            "basis": basis,
            "tonight_suggestion": suggestion,
            "trend_label": trend,
        })
    grounded["source"] = "llm"
    return grounded


async def request_llm_chat(messages: list, max_tokens: int, temperature: float = 0.7) -> str:
    """Call an OpenAI-compatible endpoint with one hard end-to-end deadline."""
    overall = max(0.1, config.LLM_OVERALL_TIMEOUT_SECONDS)
    timeout = httpx.Timeout(
        timeout=overall,
        connect=min(max(0.1, config.LLM_CONNECT_TIMEOUT_SECONDS), overall),
        read=min(max(0.1, config.LLM_READ_TIMEOUT_SECONDS), overall),
        write=min(max(0.1, config.LLM_READ_TIMEOUT_SECONDS), overall),
        pool=min(1.0, overall),
    )
    base_url = config.LLM_BASE_URL.rstrip("/")
    # Ark Coding Plan's OpenAI-compatible endpoint is /api/coding/v3. Accept
    # the older /v1 value from existing deployments and normalize it safely.
    if base_url.endswith("/api/coding/v1"):
        base_url = base_url[:-2] + "v3"
    url = base_url
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    payload = {
        "model": config.LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {"Authorization": f"Bearer {config.LLM_API_KEY}"}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        response = await asyncio.wait_for(
            client.post(url, json=payload, headers=headers),
            timeout=overall,
        )
        response.raise_for_status()
        data = response.json()
    message = data["choices"][0]["message"]
    text = (message.get("content") or message.get("reasoning_content") or "").strip()
    if not text:
        raise ValueError("empty LLM response")
    return text


async def generate_ai_interpretation(report_context: dict) -> dict:
    """
    调用大模型生成 AI 解读
    返回 ai_interpretation_v1 结构
    """
    # 如果没有配置 LLM key，用规则版兜底
    if not config.LLM_API_KEY:
        return _rule_based_interpretation(report_context)

    try:
        prompt = f"""你是酣眠 SnoozMate 的睡眠健康助手。请根据用户的睡眠数据，生成一份晨间解读。

合规要求（必须遵守，否则违规）：
- 绝对不能说：守护、监测、引导翻身、止鼾、筛查、分级、AHI、治疗、提高
- 必须用：观察、记录、趋势、轻柔提示、响应率、建议就医
- 不做医疗诊断，不是医疗器械，只提供睡眠健康趋势观察
- 语气温暖、安心，让用户感到"被陪伴"而不是"被监督"

内容要求：
1. 一句话总结昨夜（30字以内）
2. 列出 2-3 条数据依据（来自输入数据）
3. 提一个具体的小建议（20字以内）
4. 评估整体趋势（improving / stable / worsening）
5. 判断是否需要调整参数（如需，必须在安全边界内）

输入数据：
{json.dumps(report_context, ensure_ascii=False, indent=2)}

输出严格的 JSON 格式（不要任何额外文字或 markdown）：
{{
  "summary": "一句话总结昨夜（30字以内）",
  "basis": ["依据1", "依据2", "依据3"],
  "tonight_suggestion": "一个具体的小建议（20字以内）",
  "trend_label": "improving / stable / worsening",
  "config_suggestion": {{
    "relevant": true/false,
    "summary": "是否建议调整参数（一句话）",
    "params_to_adjust": {{
      "start_vibration_level": 1,
      "cooldown_seconds": 600
    }},
    "reason": "为什么建议调整（基于数据）"
  }}
}}"""

        text = await request_llm_chat(
            [{"role": "user", "content": prompt}],
            max_tokens=400,
        )
        return ground_ai_interpretation(_parse_ai_response(text), report_context)

    except (httpx.HTTPError, asyncio.TimeoutError, KeyError, IndexError, TypeError, ValueError):
        # Do not leak provider/network details to clients; return promptly and predictably.
        result = _rule_based_interpretation(report_context)
        result["source"] = "rule_based_fallback"
        return result


def _rule_based_interpretation(ctx: dict) -> dict:
    """规则版兜底解读（无 LLM 时用）"""
    daily = ctx.get("daily_summary", {})
    weekly = ctx.get("weekly_trend", {})

    event_count = daily.get("event_count", 0)
    total = daily.get("total_rounds", 0)
    success_rate = weekly.get("success_rate", 0.5)
    trend = weekly.get("trend", "stable")
    max_lvl = daily.get("max_vibration_level", 0)

    if event_count == 0:
        summary = "昨夜暂无有效睡眠记录"
        basis = ["云端尚未收到设备的结构化夜间事件"]
        suggestion = "确认设备在线后完成一晚记录"
    elif total == 0:
        summary = "昨夜记录完整，未出现需要提醒的趋势"
        basis = [f"共收到 {event_count} 条结构化事件", "没有形成有效提醒轮次"]
        suggestion = "继续保持规律作息"
    elif success_rate > 0.8:
        summary = f"昨夜干预{total}次，大多有效"
        basis = [f"共收到 {event_count} 条事件", f"成功率 {int(success_rate*100)}%", f"最高 L{max_lvl} 级"]
        suggestion = "可以试试更温和的强度"
    elif success_rate > 0.5:
        summary = f"昨夜打呼 {total} 次，效果中等"
        basis = [f"共收到 {event_count} 条事件", f"成功率 {int(success_rate*100)}%", f"最高 L{max_lvl} 级"]
        suggestion = "侧卧能减轻不少哦"
    else:
        summary = f"昨夜打呼 {total} 次，效果一般"
        basis = [f"共收到 {event_count} 条事件", f"成功率 {int(success_rate*100)}%", "可能需要更强干预"]
        suggestion = "建议做一次专业检查"

    return {
        "summary": summary,
        "basis": basis,
        "tonight_suggestion": suggestion,
        "trend_label": trend,
        "config_suggestion": {
            "relevant": False,
            "summary": "保持当前参数即可",
            "params_to_adjust": {},
            "reason": "数据不足或效果正常",
        },
        "has_data": event_count > 0,
        "source": "rule_based",
    }


def generate_rule_interpretation(ctx: dict) -> dict:
    """Public deterministic path used by latency-sensitive cached report reads."""
    return _rule_based_interpretation(ctx)


def generate_chat_fallback(message: str, context: dict) -> dict:
    """Return useful structured guidance when no external model is available."""
    daily = context.get("daily_summary", {})
    weekly = context.get("weekly_trend", {})
    event_count = int(daily.get("event_count") or 0)
    total = int(daily.get("total_rounds") or 0)
    rate = float(daily.get("success_rate") or weekly.get("success_rate") or 0)
    question = message.lower()

    if any(word in question for word in ("急救", "胸痛", "呼吸困难", "emergency")):
        return {
            "answer": "如果正在出现胸痛、呼吸困难或其他紧急不适，请立即联系当地急救服务，不要等待应用分析。",
            "sections": {
                "canExplain": "应用只能说明已有睡眠记录中的变化。",
                "cannotDetermine": "应用不能判断紧急症状的原因或严重程度。",
                "nextStep": "立即联系急救服务，并告知身边的人。",
            },
            "answer_kind": "safety",
            "safety_class": "urgent",
        }

    if any(word in question for word in ("吃药", "用药", "停药", "剂量", "诊断", "呼吸暂停")):
        return {
            "answer": "这些记录不能用于诊断，也不能据此调整药物。可以把连续几晚的报告和白天感受整理后交给医生参考。",
            "sections": {
                "canExplain": "可以说明已记录的提醒次数、响应率和连续趋势。",
                "cannotDetermine": "不能判断疾病，也不能给出用药或停药建议。",
                "nextStep": "如有憋醒、持续困倦等不适，请咨询医生。",
            },
            "answer_kind": "safety",
            "safety_class": "medical",
        }

    if event_count == 0:
        return {
            "answer": "当前还没有收到有效夜间记录，所以暂时无法分析改善方向。请先确认月石主机在线、传感器就绪，并完成一晚记录。",
            "sections": {
                "canExplain": "目前只能确认云端尚未收到这台设备的结构化夜间事件。",
                "cannotDetermine": "没有数据时无法判断鼾声趋势、体位变化或提醒效果。",
                "nextStep": "检查设备连接与同步状态，完成一晚记录后再询问。",
            },
            "answer_kind": "data-unavailable",
            "safety_class": "trend",
        }

    if any(word in question for word in ("怎么办", "改善", "怎么做", "建议")):
        if total == 0:
            suggestion = "昨夜有有效记录，但没有形成提醒轮次。先保持当前设置并继续观察，不需要主动调高强度。"
        elif rate >= 0.7:
            suggestion = f"昨夜共记录 {total} 轮提醒，响应率约 {int(rate * 100)}%。可以保持当前温和设置，并优先维持规律作息和舒适侧卧。"
        else:
            suggestion = f"昨夜共记录 {total} 轮提醒，响应率约 {int(rate * 100)}%。先检查佩戴与放置位置，连续观察几晚后再决定是否调整设置。"
        return {
            "answer": suggestion,
            "sections": {
                "canExplain": f"昨夜收到 {event_count} 条事件，形成 {total} 轮提醒。",
                "cannotDetermine": "单晚记录不能用于医疗诊断，也不能证明某种睡眠疾病。",
                "nextStep": "保持设备在线并连续记录至少三晚，再结合晨间感受观察。",
            },
            "answer_kind": "trend",
            "safety_class": "trend",
        }

    return {
        "answer": f"根据已记录的数据，昨夜收到 {event_count} 条事件，形成 {total} 轮提醒，响应率约 {int(rate * 100)}%。你可以继续问改善建议、响应变化或今晚设置。",
        "sections": {
            "canExplain": "可以解释提醒次数、响应率和连续趋势。",
            "cannotDetermine": "不能据此进行医疗诊断。",
            "nextStep": "继续记录并结合晨间感受查看趋势。",
        },
        "answer_kind": "trend",
        "safety_class": "trend",
    }
