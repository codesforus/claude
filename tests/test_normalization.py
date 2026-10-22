from datetime import datetime
from zoneinfo import ZoneInfo

from leadgen.phones import domain_of, normalize_email, normalize_ico, normalize_phone
from leadgen.settings import CallingCfg
from leadgen.timeutil import CallingCalendar

PRAGUE = ZoneInfo("Europe/Prague")


def test_phone_normalization():
    assert normalize_phone("602 123 456") == "+420602123456"
    assert normalize_phone("+420 224 812 345") == "+420224812345"
    assert normalize_phone("00420 602 123 456") == "+420602123456"
    assert normalize_phone("123") is None
    assert normalize_phone("") is None
    assert normalize_phone("900 123 456") is None  # premium rate


def test_domain_and_ids():
    assert domain_of("https://www.Firma-Topeni.cz/kontakt") == "firma-topeni.cz"
    assert domain_of("firma.cz") == "firma.cz"
    assert domain_of("https://www.facebook.com/firma") is None
    assert domain_of("https://www.firmy.cz/detail/123") is None
    assert normalize_ico("123 45 678") == "12345678"
    assert normalize_ico("4567") == "00004567"
    assert normalize_ico("123456789") is None
    assert normalize_email(" Jan.Novak@Firma.CZ ") == "jan.novak@firma.cz"
    assert normalize_email("jan zavináč firma") is None


def _cal():
    return CallingCalendar(CallingCfg(windows=["09:00-11:45", "13:00-16:30"]), "Europe/Prague")


def test_calling_windows():
    cal = _cal()
    assert cal.in_window(datetime(2026, 10, 22, 10, 0, tzinfo=PRAGUE))  # Thursday morning
    assert not cal.in_window(datetime(2026, 10, 22, 12, 0, tzinfo=PRAGUE))  # lunch
    assert not cal.in_window(datetime(2026, 10, 24, 10, 0, tzinfo=PRAGUE))  # Saturday
    assert not cal.in_window(datetime(2026, 10, 28, 10, 0, tzinfo=PRAGUE))  # state holiday


def test_next_slot():
    cal = _cal()
    lunch = datetime(2026, 10, 22, 12, 0, tzinfo=PRAGUE)
    assert cal.local(cal.next_slot(lunch)) == datetime(2026, 10, 22, 13, 0, tzinfo=PRAGUE)
    friday_evening = datetime(2026, 10, 23, 17, 0, tzinfo=PRAGUE)
    assert cal.local(cal.next_slot(friday_evening)) == datetime(2026, 10, 26, 9, 0, tzinfo=PRAGUE)
    before_holiday = datetime(2026, 10, 27, 17, 0, tzinfo=PRAGUE)
    assert cal.local(cal.next_slot(before_holiday)) == datetime(2026, 10, 29, 9, 0, tzinfo=PRAGUE)
    inside = datetime(2026, 10, 22, 10, 15, tzinfo=PRAGUE)
    assert cal.local(cal.next_slot(inside)) == inside


def test_cron_uses_standard_weekdays():
    from leadgen.scheduler import cron_trigger

    friday_7am = datetime(2026, 10, 23, 7, 0, tzinfo=PRAGUE)
    workdays = cron_trigger("0 6 * * 1-5", "Europe/Prague").get_next_fire_time(None, friday_7am)
    assert workdays == datetime(2026, 10, 26, 6, 0, tzinfo=PRAGUE)  # Monday, not Saturday
    weekly = cron_trigger("30 7 * * 1", "Europe/Prague").get_next_fire_time(None, friday_7am)
    assert weekly == datetime(2026, 10, 26, 7, 30, tzinfo=PRAGUE)
    sunday = cron_trigger("0 20 * * 0", "Europe/Prague").get_next_fire_time(None, friday_7am)
    assert sunday.date().isoweekday() == 7
