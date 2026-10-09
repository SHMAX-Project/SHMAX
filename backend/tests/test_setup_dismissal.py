"""Dismissal records a UI preference; it grants neither readiness nor authorization."""
from tests.test_setup_complete_enforcement import booted_client
from tests.test_bootstrap_takeover import fresh_app


def test_skip_retains_owner_and_incomplete_configuration(booted_client):
    before = booted_client.get('/api/status').json()
    workers = tuple(booted_client.app.state.runtime._worker_tasks)
    result = booted_client.post('/api/setup/dismiss')
    assert result.status_code == 200
    session = booted_client.get('/api/auth/session').json()
    assert session['authenticated'] and session['username'] == 'owner'
    assert session['setup_dismissed'] is True and session['setup_complete'] is False
    after = booted_client.get('/api/status').json()
    for component in ('max', 'telegram', 'bridge', 'queue'):
        assert after[component] == before[component]
    assert tuple(booted_client.app.state.runtime._worker_tasks) == workers
    assert booted_client.post('/api/setup/complete').status_code == 409


def test_skip_persists_across_logout_and_login(booted_client):
    assert booted_client.post('/api/setup/dismiss').status_code == 200
    assert booted_client.post('/api/auth/logout').status_code == 200
    assert booted_client.post('/api/setup/dismiss').status_code == 401
    assert booted_client.post('/api/auth/login', json={'username': 'owner', 'password': 'owner-pass-12345'}).status_code == 200
    session = booted_client.get('/api/auth/session').json()
    assert session['setup_dismissed'] and not session['setup_complete']


def test_skip_requires_csrf(booted_client):
    del booted_client.headers['X-CSRF-Token']
    assert booted_client.post('/api/setup/dismiss').status_code == 403
    assert booted_client.get('/api/auth/session').json()['setup_dismissed'] is False


def test_skip_does_not_clear_completed_setup(booted_client):
    assert booted_client.post('/api/setup/complete', json={'acknowledge_unconfigured': True}).status_code == 200
    assert booted_client.post('/api/setup/dismiss').status_code == 200
    assert booted_client.get('/api/auth/session').json()['setup_complete'] is True


def test_skip_cannot_replace_initial_admin_creation(fresh_app):
    assert fresh_app.post('/api/setup/dismiss').status_code == 401
    session = fresh_app.get('/api/auth/session').json()
    assert session['admin_exists'] is False and session['authenticated'] is False
    assert session['setup_dismissed'] is False and session['setup_complete'] is False
