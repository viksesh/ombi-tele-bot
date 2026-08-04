"""Tests for TV request submission (v2 TMDB endpoint with v1 fallback)."""

import pytest
from unittest.mock import MagicMock, patch

with patch.dict('os.environ', {
    'OMBI_URL': 'http://fake-ombi',
    'OMBI_API_KEY': 'fake-key',
    'NZB_API_KEY': '',  # Disable NZB client
}):
    import media_service


@pytest.fixture
def ombi():
    """Swap in a mock Ombi client for the duration of a test."""
    client = MagicMock()
    with patch.object(media_service, 'ombi_client', client):
        yield client


class TestSubmitTvRequest:
    """v2 (TMDB) is preferred; v1 (TVDB) is the fallback."""

    def test_uses_v2_when_item_has_tmdb_id(self, ombi):
        ombi.request_tv_v2.return_value = True
        item = {'theTvDbId': 480111, 'theMovieDbId': '317340'}

        assert media_service._submit_tv_request(480111, item, None) is True
        ombi.request_tv_v2.assert_called_once_with('317340', None)
        ombi.request_tv.assert_not_called()

    def test_falls_back_to_v1_when_v2_fails(self, ombi):
        ombi.request_tv_v2.return_value = False
        ombi.request_tv.return_value = True
        item = {'theTvDbId': 480111, 'theMovieDbId': '317340'}

        assert media_service._submit_tv_request(480111, item, 'requests-auto') is True
        ombi.request_tv.assert_called_once_with(480111, 'requests-auto')

    def test_uses_v1_when_no_tmdb_id(self, ombi):
        """v1/TVMaze-backed results have no theMovieDbId."""
        ombi.request_tv.return_value = True

        assert media_service._submit_tv_request(279121, {'theTvDbId': 279121}, None) is True
        ombi.request_tv_v2.assert_not_called()
        ombi.request_tv.assert_called_once_with(279121, None)

    def test_uses_v1_when_item_is_missing(self, ombi):
        """Stale mini app session: no raw item to read the TMDB id from."""
        ombi.request_tv.return_value = True

        assert media_service._submit_tv_request(279121, None, None) is True
        ombi.request_tv_v2.assert_not_called()


class TestRequestSucceeded:
    """A 2xx from Ombi can still carry a rejection in the body."""

    def test_none_is_failure(self):
        from ombi_client import OmbiClient
        assert OmbiClient._request_succeeded(None) is False

    def test_result_false_is_failure(self):
        from ombi_client import OmbiClient
        assert OmbiClient._request_succeeded({'result': False, 'errorMessage': 'nope'}) is False

    def test_result_true_is_success(self):
        from ombi_client import OmbiClient
        assert OmbiClient._request_succeeded({'result': True}) is True

    def test_empty_body_is_success(self):
        from ombi_client import OmbiClient
        assert OmbiClient._request_succeeded({}) is True
