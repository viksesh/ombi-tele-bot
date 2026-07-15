"""Tests for Sonarr-cache availability detection (get_item_status / _tv_aired_coverage).

Shows that exist in Sonarr with files but were never requested through Ombi
come back from the v2 API with show-level available=True and per-episode
approved=True, but per-episode available=False (that flag is media-server
only). These tests cover the coverage heuristic that turns that data into
available / partially_available.
"""

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta

with patch.dict('os.environ', {
    'TELEGRAM_BOT_TOKEN': 'fake-token',
    'OMBI_URL': 'http://fake-ombi',
    'OMBI_API_KEY': 'fake-key',
    'NZB_API_KEY': '',  # Disable NZB client
}):
    import sys
    sys.modules['telegram'] = MagicMock()
    sys.modules['telegram.ext'] = MagicMock()
    sys.modules['telegram.error'] = MagicMock()

    from media_service import get_item_status, _tv_aired_coverage


def _ep(number, aired=True, approved=True, available=False, air_date=None):
    if air_date is None:
        delta = timedelta(days=-30) if aired else timedelta(days=30)
        air_date = (datetime.now() + delta).strftime('%Y-%m-%dT00:00:00')
    return {
        'episodeNumber': number,
        'airDate': air_date,
        'approved': approved,
        'available': available,
        'requested': False,
    }


def _show(seasons, **flags):
    item = {
        'title': 'Test Show',
        'theTvDbId': '443433',
        'available': False,
        'fullyAvailable': False,
        'partlyAvailable': False,
        'approved': False,
        'requested': False,
        'requestId': 0,
        'seasonRequests': seasons,
    }
    item.update(flags)
    return item


class TestTvAiredCoverage:
    def test_all_aired_covered(self):
        seasons = [{'seasonNumber': 1, 'episodes': [_ep(1), _ep(2), _ep(3)]}]
        assert _tv_aired_coverage(_show(seasons)) == (3, 3)

    def test_partial_coverage(self):
        seasons = [
            {'seasonNumber': 1, 'episodes': [_ep(1), _ep(2)]},
            {'seasonNumber': 2, 'episodes': [_ep(1, approved=False), _ep(2, approved=False)]},
        ]
        assert _tv_aired_coverage(_show(seasons)) == (4, 2)

    def test_specials_excluded(self):
        seasons = [
            {'seasonNumber': 0, 'episodes': [_ep(1, approved=False)]},
            {'seasonNumber': 1, 'episodes': [_ep(1), _ep(2)]},
        ]
        assert _tv_aired_coverage(_show(seasons)) == (2, 2)

    def test_unaired_excluded(self):
        seasons = [{'seasonNumber': 1, 'episodes': [
            _ep(1), _ep(2), _ep(3, aired=False, approved=False),
        ]}]
        assert _tv_aired_coverage(_show(seasons)) == (2, 2)

    def test_missing_air_date_excluded(self):
        seasons = [{'seasonNumber': 1, 'episodes': [
            _ep(1),
            _ep(2, approved=False, air_date=''),
            _ep(3, approved=False, air_date='0001-01-01T00:00:00'),
        ]}]
        assert _tv_aired_coverage(_show(seasons)) == (1, 1)

    def test_no_episode_data(self):
        assert _tv_aired_coverage(_show([])) is None
        assert _tv_aired_coverage(_show(None)) is None
        assert _tv_aired_coverage({'title': 'Movie'}) is None

    def test_media_server_available_counts_as_covered(self):
        seasons = [{'seasonNumber': 1, 'episodes': [
            _ep(1, approved=False, available=True),
        ]}]
        assert _tv_aired_coverage(_show(seasons)) == (1, 1)


class TestGetItemStatusSonarrAvailability:
    def test_fully_in_sonarr_never_requested(self):
        """Show in Sonarr with all aired episodes monitored + a file -> available."""
        seasons = [
            {'seasonNumber': 1, 'episodes': [_ep(1), _ep(2)]},
            {'seasonNumber': 2, 'episodes': [_ep(1), _ep(2)]},
        ]
        item = _show(seasons, available=True, approved=True)
        should_hide, status = get_item_status(item)
        assert should_hide is True
        assert status == 'available'

    def test_partially_in_sonarr(self):
        """Show with a file in Sonarr but unmonitored aired episodes -> partially available."""
        seasons = [
            {'seasonNumber': 1, 'episodes': [_ep(1), _ep(2)]},
            {'seasonNumber': 2, 'episodes': [_ep(1, approved=False), _ep(2, approved=False)]},
        ]
        item = _show(seasons, available=True, approved=True)
        should_hide, status = get_item_status(item)
        assert should_hide is True
        assert status == 'partially_available'

    def test_caught_up_continuing_show(self):
        """Unaired future episodes must not downgrade a caught-up show."""
        seasons = [{'seasonNumber': 1, 'episodes': [
            _ep(1), _ep(2), _ep(3, aired=False, approved=False),
        ]}]
        item = _show(seasons, available=True, approved=True)
        should_hide, status = get_item_status(item)
        assert should_hide is True
        assert status == 'available'

    def test_fully_available_flag_short_circuits(self):
        """Media-server fullyAvailable wins regardless of coverage."""
        seasons = [{'seasonNumber': 1, 'episodes': [_ep(1, approved=False)]}]
        item = _show(seasons, fullyAvailable=True)
        should_hide, status = get_item_status(item)
        assert should_hide is True
        assert status == 'available'

    def test_available_without_episode_data(self):
        item = _show([], available=True)
        should_hide, status = get_item_status(item)
        assert should_hide is True
        assert status == 'available'

    def test_in_sonarr_no_files_yet(self):
        """Show monitored in Sonarr but nothing downloaded -> approved (processing)."""
        seasons = [{'seasonNumber': 1, 'episodes': [_ep(1), _ep(2)]}]
        item = _show(seasons, approved=True)
        should_hide, status = get_item_status(item)
        assert should_hide is True
        assert status == 'approved'

    def test_not_in_sonarr_requestable(self):
        seasons = [{'seasonNumber': 1, 'episodes': [
            _ep(1, approved=False), _ep(2, approved=False),
        ]}]
        item = _show(seasons)
        should_hide, status = get_item_status(item)
        assert should_hide is False
        assert status is None

    def test_movie_available_unaffected(self):
        item = {'title': 'Movie', 'available': True}
        should_hide, status = get_item_status(item)
        assert should_hide is True
        assert status == 'available'

    def test_denied_beats_available(self):
        seasons = [{'seasonNumber': 1, 'episodes': [_ep(1)]}]
        item = _show(seasons, available=True, denied=True)
        should_hide, status = get_item_status(item)
        assert should_hide is True
        assert status == 'denied'

    def test_season_level_denied_detected(self):
        """Ombi denies TV per-season; no show-level denied flag is set."""
        seasons = [{'seasonNumber': 1, 'denied': True, 'episodes': [_ep(1)]}]
        item = _show(seasons, requestId=42, requested=True)
        should_hide, status = get_item_status(item)
        assert should_hide is True
        assert status == 'denied'

    def test_episode_level_denied_detected(self):
        seasons = [{'seasonNumber': 1, 'episodes': [
            dict(_ep(1), denied=True),
        ]}]
        item = _show(seasons, requestId=42, requested=True)
        should_hide, status = get_item_status(item)
        assert should_hide is True
        assert status == 'denied'

    def test_no_denied_season_still_requestable(self):
        seasons = [{'seasonNumber': 1, 'denied': False, 'episodes': [_ep(1, aired=False)]}]
        item = _show(seasons)
        should_hide, status = get_item_status(item)
        assert should_hide is False
        assert status is None
