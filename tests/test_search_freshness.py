"""Свежесть веб-поиска: период для DuckDuckGo и «месяц год» в запросе."""
import datetime

from routes.search import freshen_search_query, search_time_filter


def test_week_news_uses_week_filter():
    assert search_time_filter("Найди 3 новости про ии за эту неделю") == "w"


def test_today_uses_day_filter():
    assert search_time_filter("новости ИИ сегодня") == "d"


def test_timeless_query_has_no_filter():
    assert search_time_filter("что такое python") is None


def test_freshen_adds_month_and_year():
    today = datetime.date(2026, 11, 5)
    assert freshen_search_query("новости ИИ за неделю", today=today).endswith("ноябрь 2026")


def test_freshen_keeps_explicit_year_and_timeless_queries():
    today = datetime.date(2026, 11, 5)
    assert freshen_search_query("новости ИИ 2024", today=today) == "новости ИИ 2024"
    assert freshen_search_query("что такое python", today=today) == "что такое python"
