"""Tests for the estimated-availability date (get_expected_date).

Movies get Ombi's digital release date; TV shows get the next episode still to
air. Dates already in the past are dropped - an approved item whose date has
passed is just waiting on a download, and the stale date would mislead.
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

    from media_service import get_expected_date


def _offset(days):
    return (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%dT00:00:00Z')


def _iso(days):
    return (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d')


class TestMovies:
    def test_future_digital_release(self):
        item = {'digitalReleaseDate': _offset(30)}
        assert get_expected_date(item, 'movie') == _iso(30)

    def test_today_counts_as_upcoming(self):
        item = {'digitalReleaseDate': _offset(0)}
        assert get_expected_date(item, 'movie') == _iso(0)

    def test_past_digital_release_dropped(self):
        item = {'digitalReleaseDate': _offset(-1)}
        assert get_expected_date(item, 'movie') is None

    def test_missing_date(self):
        assert get_expected_date({}, 'movie') is None
        assert get_expected_date({'digitalReleaseDate': None}, 'movie') is None

    def test_ombi_null_sentinel(self):
        item = {'digitalReleaseDate': '0001-01-01T00:00:00Z'}
        assert get_expected_date(item, 'movie') is None

    def test_malformed_date(self):
        assert get_expected_date({'digitalReleaseDate': 'soon'}, 'movie') is None
        assert get_expected_date({'digitalReleaseDate': 12345}, 'movie') is None


def _season(number, *air_dates):
    return {
        'seasonNumber': number,
        'episodes': [{'episodeNumber': i + 1, 'airDate': d} for i, d in enumerate(air_dates)],
    }


class TestTvShows:
    def test_unpremiered_show_gets_premiere_date(self):
        # Every episode is still in the future -> a genuinely new show
        item = {'seasonRequests': [_season(1, _offset(20), _offset(27), _offset(34))]}
        assert get_expected_date(item, 'tv') == _iso(20)

    def test_premiere_is_earliest_across_seasons(self):
        item = {'seasonRequests': [
            _season(2, _offset(40)),
            _season(1, _offset(20), _offset(27)),
        ]}
        assert get_expected_date(item, 'tv') == _iso(20)

    def test_show_with_any_aired_episode_has_no_date(self):
        # Once one episode has aired the show is grabbable/available, not "new"
        item = {'seasonRequests': [_season(1, _offset(-7), _offset(3), _offset(10))]}
        assert get_expected_date(item, 'tv') is None

    def test_specials_ignored(self):
        # A special airing in the past must not suppress an unpremiered show
        item = {'seasonRequests': [
            _season(0, _offset(-2)),
            _season(1, _offset(9)),
        ]}
        assert get_expected_date(item, 'tv') == _iso(9)

    def test_fully_aired_show_has_no_date(self):
        item = {'seasonRequests': [_season(1, _offset(-30), _offset(-14))]}
        assert get_expected_date(item, 'tv') is None

    def test_undated_episodes_ignored(self):
        item = {'seasonRequests': [_season(1, None, '', '0001-01-01T00:00:00', _offset(6))]}
        assert get_expected_date(item, 'tv') == _iso(6)

    def test_no_episode_data(self):
        assert get_expected_date({}, 'tv') is None
        assert get_expected_date({'seasonRequests': []}, 'tv') is None
        assert get_expected_date({'seasonRequests': 'invalid'}, 'tv') is None
        assert get_expected_date({'seasonRequests': [{'seasonNumber': 1}]}, 'tv') is None


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
