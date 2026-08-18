"""Tests for the per-user request history (storage + live status refresh)."""

import importlib
import os
import time

import pytest
from unittest.mock import MagicMock, patch

with patch.dict('os.environ', {
    'OMBI_URL': 'http://fake-ombi',
    'OMBI_API_KEY': 'fake-key',
    'NZB_API_KEY': '',  # Disable NZB client
}):
    import media_service


@pytest.fixture
def history(tmp_path):
    """A fresh request_history module backed by a temporary database."""
    import request_history

    with patch.dict('os.environ', {
        'REQUEST_HISTORY_DB': str(tmp_path / 'requests.db'),
        'REQUEST_HISTORY_DAYS': '30',
    }):
        yield importlib.reload(request_history)


@pytest.fixture
def ombi():
    """Swap in a mock Ombi client for the duration of a test."""
    client = MagicMock()
    media_service._request_state_cache.clear()
    with patch.object(media_service, 'ombi_client', client):
        yield client
    media_service._request_state_cache.clear()


class TestStorage:

    def test_records_and_lists_a_request(self, history):
        history.record_request(42, 'movie', 603, 'The Matrix', year=1999,
                               poster='http://img/p.jpg', status='requested')

        entries = history.list_requests(42)
        assert len(entries) == 1
        assert entries[0]['id'] == '603'
        assert entries[0]['type'] == 'movie'
        assert entries[0]['title'] == 'The Matrix'
        assert entries[0]['year'] == 1999
        assert entries[0]['status'] == 'requested'
        assert entries[0]['requestedAt'].endswith('+00:00')

    def test_users_only_see_their_own_requests(self, history):
        history.record_request(1, 'movie', 603, 'The Matrix')
        history.record_request(2, 'tv', 81189, 'Breaking Bad')

        assert [e['title'] for e in history.list_requests(1)] == ['The Matrix']
        assert [e['title'] for e in history.list_requests(2)] == ['Breaking Bad']

    def test_newest_first(self, history):
        history.record_request(1, 'movie', 1, 'Older')
        time.sleep(1.1)  # timestamps have second resolution
        history.record_request(1, 'movie', 2, 'Newer')

        assert [e['title'] for e in history.list_requests(1)] == ['Newer', 'Older']

    def test_re_requesting_updates_the_existing_entry(self, history):
        history.record_request(1, 'movie', 603, 'The Matrix', status='requested')
        history.record_request(1, 'movie', 603, 'The Matrix', status='approved',
                               auto_approved=True)

        entries = history.list_requests(1)
        assert len(entries) == 1
        assert entries[0]['status'] == 'approved'
        assert entries[0]['autoApproved'] is True

    def test_entries_older_than_the_retention_window_are_dropped(self, history):
        history.record_request(1, 'movie', 603, 'The Matrix')
        history.record_request(1, 'movie', 604, 'Old Movie')
        # Backdate one entry past the 30-day window
        with history._connect() as conn:
            conn.execute("UPDATE requests SET requested_at = ? WHERE item_id = '604'",
                         (int(time.time()) - 31 * 86400,))

        assert [e['title'] for e in history.list_requests(1)] == ['The Matrix']

    def test_default_retention_is_60_days(self, tmp_path):
        import request_history

        with patch.dict('os.environ', {'REQUEST_HISTORY_DB': str(tmp_path / 'default.db')}):
            os.environ.pop('REQUEST_HISTORY_DAYS', None)  # restored by patch.dict
            assert importlib.reload(request_history).RETENTION_DAYS == 60

    def test_zero_retention_keeps_everything(self, tmp_path):
        import request_history

        with patch.dict('os.environ', {
            'REQUEST_HISTORY_DB': str(tmp_path / 'forever.db'),
            'REQUEST_HISTORY_DAYS': '0',
        }):
            store = importlib.reload(request_history)
            store.record_request(1, 'movie', 603, 'The Matrix')
            with store._connect() as conn:
                conn.execute("UPDATE requests SET requested_at = ?",
                             (int(time.time()) - 3650 * 86400,))

            assert len(store.list_requests(1)) == 1

    def test_storage_failures_do_not_raise(self, history):
        with patch.object(history, '_connect', side_effect=OSError('disk gone')):
            history.record_request(1, 'movie', 603, 'The Matrix')  # must not raise
            assert history.list_requests(1) == []


class TestRequestStateDerivation:

    def test_movie_states(self):
        assert media_service._movie_request_state({'denied': True, 'available': True})['status'] == 'denied'
        assert media_service._movie_request_state({'available': True})['status'] == 'available'
        assert media_service._movie_request_state({'approved': True})['status'] == 'approved'
        assert media_service._movie_request_state({})['status'] == 'requested'

    def test_movie_approved_carries_expected_date(self):
        future = f"{time.strftime('%Y', time.gmtime(time.time() + 400 * 86400))}-12-31T00:00:00"
        state = media_service._movie_request_state({'approved': True, 'digitalReleaseDate': future})
        assert state['expectedDate'] == future[:10]

    def test_tv_denied_beats_availability(self):
        req = {'childRequests': [{'denied': True, 'approved': True}]}
        assert media_service._tv_request_state(req)['status'] == 'denied'

    def test_tv_fully_available(self):
        req = {'childRequests': [{'approved': True, 'seasonRequests': [
            {'episodes': [{'available': True}, {'available': True}]},
        ]}]}
        assert media_service._tv_request_state(req)['status'] == 'available'

    def test_tv_partially_available(self):
        req = {'childRequests': [{'approved': True, 'seasonRequests': [
            {'episodes': [{'available': True}, {'available': False}]},
        ]}]}
        assert media_service._tv_request_state(req)['status'] == 'partially_available'

    def test_tv_approved_but_nothing_downloaded(self):
        req = {'childRequests': [{'approved': True, 'seasonRequests': [
            {'episodes': [{'available': False}]},
        ]}]}
        assert media_service._tv_request_state(req)['status'] == 'approved'

    def test_tv_pending_approval(self):
        assert media_service._tv_request_state({'childRequests': [{}]})['status'] == 'requested'


class TestRefreshRequestStatuses:

    def test_updates_stored_status_from_ombi(self, ombi):
        ombi.get_movie_requests.return_value = [{'theMovieDbId': 603, 'available': True}]
        ombi.get_tv_requests.return_value = [{'tvDbId': 81189, 'childRequests': [{'denied': True}]}]

        entries = [
            {'id': '603', 'type': 'movie', 'status': 'requested'},
            {'id': '81189', 'type': 'tv', 'status': 'requested'},
        ]
        media_service.refresh_request_statuses(entries)

        assert entries[0]['status'] == 'available'
        assert entries[1]['status'] == 'denied'

    def test_keeps_stored_status_when_ombi_has_no_record(self, ombi):
        ombi.get_movie_requests.return_value = []

        entries = [{'id': '603', 'type': 'movie', 'status': 'approved'}]
        media_service.refresh_request_statuses(entries)

        assert entries[0]['status'] == 'approved'
        assert entries[0]['expectedDate'] is None

    def test_keeps_stored_status_when_ombi_errors(self, ombi):
        ombi.get_movie_requests.side_effect = RuntimeError('ombi down')

        entries = [{'id': '603', 'type': 'movie', 'status': 'requested'}]
        media_service.refresh_request_statuses(entries)

        assert entries[0]['status'] == 'requested'

    def test_fetches_each_media_type_once(self, ombi):
        ombi.get_movie_requests.return_value = []

        entries = [
            {'id': '1', 'type': 'movie', 'status': 'requested'},
            {'id': '2', 'type': 'movie', 'status': 'requested'},
        ]
        media_service.refresh_request_statuses(entries)

        ombi.get_movie_requests.assert_called_once()
        ombi.get_tv_requests.assert_not_called()
