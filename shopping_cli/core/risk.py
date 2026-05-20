"""Risk and automation-boundary classification for consultations."""

from __future__ import annotations


BARGAINING_TERMS = {
    "discount",
    "cheaper",
    "private price",
    "bargain",
    "优惠",
    "便宜",
    "降价",
    "折扣",
    "私下",
    "少一点",
}

UNSUPPORTED_TERMS = {
    "order",
    "payment",
    "pay",
    "refund",
    "escrow",
    "reserve",
    "delivery success",
    "下单",
    "订单",
    "支付",
    "付款",
    "退款",
    "担保",
    "预留",
    "保留库存",
}

SUSPICIOUS_TERMS = {
    "counterfeit",
    "fake id",
    "forged",
    "fraud",
    "scam",
    "illegal",
    "假货",
    "假证",
    "伪造",
    "诈骗",
    "违法",
}

LOW_CONFIDENCE_TERMS = {
    "maybe",
    "not sure",
    "uncertain",
    "low confidence",
    "不确定",
    "不清楚",
    "随便",
}


def human_review_reason(text: str, product_found: bool = True) -> str:
    lower = text.lower()
    if any(term in lower or term in text for term in BARGAINING_TERMS):
        return "bargaining"
    if any(term in lower or term in text for term in UNSUPPORTED_TERMS):
        return "unsupported_transaction"
    if any(term in lower or term in text for term in SUSPICIOUS_TERMS):
        return "suspicious_content"
    if any(term in lower or term in text for term in LOW_CONFIDENCE_TERMS):
        return "low_confidence"
    if not product_found:
        return "unclear_product"
    return ""


def infer_intent(text: str) -> str:
    lower = text.lower()
    if any(term in lower or term in text for term in ("stock", "available", "库存", "有货", "现货")):
        return "ask_stock"
    if any(term in lower or term in text for term in ("delivery", "ship", "courier", "送", "配送", "快递", "到货")):
        return "ask_delivery"
    if any(term in lower or term in text for term in ("price", "cost", "多少钱", "价格", "报价", "售价")):
        return "ask_price"
    if any(term in lower or term in text for term in BARGAINING_TERMS):
        return "negotiate"
    return "ask_product"
