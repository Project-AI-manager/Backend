"""Pydantic schemas for dashboard analytics."""

from datetime import date

from pydantic import BaseModel


class AnalyticsStatusBreakdownItem(BaseModel):
    status: str
    count: int


class AnalyticsDailySeriesItem(BaseModel):
    date: date
    dialogs: int


class AnalyticsOverviewResponse(BaseModel):
    date_from: date
    date_to: date
    dialogs_total: int
    dialogs_open: int
    dialogs_auto: int
    dialogs_escalated: int
    dialogs_closed: int
    auto_reply_rate: float
    escalation_rate: float
    avg_response_sec: int
    avg_ai_confidence: float
    ai_replies_count: int
    manager_replies_count: int
    inbound_messages_count: int
    dialogs_used: int
    dialogs_limit: int
    knowledge_documents_ready: int
    knowledge_chunks_count: int
    pending_candidates_count: int
    status_breakdown: list[AnalyticsStatusBreakdownItem]
    daily_series: list[AnalyticsDailySeriesItem]
