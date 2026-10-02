"""What paid and free users see (round prelaunch-fixes B2, design §5.5).

Until checkout exists the wording for everyone but paid users doesn't name
paid plans ([Q5], ruled 2026-10-02, round note [C1]).
"""
import pytest

from pdf_ops import office

PAID_HINT = 'Word files keep their own layout and page size.'
FREE_HINT = 'Word files are re-flowed onto your paper size.'


@pytest.fixture
def lo_on(monkeypatch):
    monkeypatch.setattr(office, 'available', lambda: True)


@pytest.mark.parametrize('page', ['/', '/convert'])
def test_anonymous_sees_the_neutral_hint(client, lo_on, page):
    html = client.get(page).get_data(as_text=True)
    assert FREE_HINT in html and PAID_HINT not in html
    assert 'paid' not in html.lower().split('wordhint')[1][:200]


@pytest.mark.parametrize('page', ['/', '/convert'])
def test_free_user_sees_the_neutral_hint(client, make_user, login, lo_on, page):
    login(client, make_user(plan='free'))
    html = client.get(page).get_data(as_text=True)
    assert FREE_HINT in html and PAID_HINT not in html


@pytest.mark.parametrize('page', ['/', '/convert'])
def test_paid_user_sees_the_engine_hint(client, make_user, login, lo_on, page):
    login(client, make_user(plan='monthly'))
    html = client.get(page).get_data(as_text=True)
    assert PAID_HINT in html and FREE_HINT not in html


def test_paid_user_without_libreoffice_is_told_the_truth(client, make_user, login,
                                                         monkeypatch):
    monkeypatch.setattr(office, 'available', lambda: False)
    login(client, make_user(plan='monthly'))
    assert FREE_HINT in client.get('/').get_data(as_text=True)


def test_account_lists_paid_features_to_paid_users(client, make_user, login, lo_on):
    login(client, make_user(plan='monthly'))
    html = client.get('/account').get_data(as_text=True)
    assert 'Your plan includes' in html
    assert 'The Word engine' in html
    assert 'Merge and Convert uploads up to 90 MB per request' in html


def test_account_shows_free_users_nothing_about_paid_plans(client, make_user, login, lo_on):
    login(client, make_user(plan='free'))
    html = client.get('/account').get_data(as_text=True)
    assert 'Your plan includes' not in html
    assert 'Word engine' not in html
    assert '90 MB' not in html
