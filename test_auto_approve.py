"""Tests for the should_auto_approve function, focusing on TV show season checks."""

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime

# Mock environment variables before importing bot
with patch.dict('os.environ', {
    'TELEGRAM_BOT_TOKEN': 'fake-token',
    'OMBI_URL': 'http://fake-ombi',
    'OMBI_API_KEY': 'fake-key',
    'NZB_API_KEY': '',  # Disable NZB client
}):
    # Mock telegram imports since we don't need them
    import sys
    sys.modules['telegram'] = MagicMock()
    sys.modules['telegram.ext'] = MagicMock()
    sys.modules['telegram.error'] = MagicMock()

    from bot import should_auto_approve, get_item_year


class TestTVSeasonCheck:
    """Test that TV shows with >3 seasons or unknown season count are NOT auto-approved."""

    def test_tv_no_season_requests_key(self):
        """TV show with no seasonRequests key should NOT be auto-approved."""
        item = {'title': 'Some Show', 'firstAired': '2020-01-01'}
        approved, reason = should_auto_approve(item, 'tv')
        assert approved is False

    def test_tv_season_requests_none(self):
        """TV show with seasonRequests=None should NOT be auto-approved."""
        item = {'title': 'Some Show', 'seasonRequests': None, 'firstAired': '2020-01-01'}
        approved, reason = should_auto_approve(item, 'tv')
        assert approved is False

    def test_tv_season_requests_empty_list(self):
        """TV show with empty seasonRequests should NOT be auto-approved."""
        item = {'title': 'Some Show', 'seasonRequests': [], 'firstAired': '2020-01-01'}
        approved, reason = should_auto_approve(item, 'tv')
        assert approved is False

    def test_tv_more_than_3_seasons(self):
        """TV show with >3 seasons should NOT be auto-approved."""
        seasons = [{'seasonNumber': i} for i in range(1, 8)]  # 7 seasons
        item = {'title': 'LEGO Ninjago', 'seasonRequests': seasons, 'firstAired': '2011-01-01'}
        approved, reason = should_auto_approve(item, 'tv')
        assert approved is False

    def test_tv_exactly_4_seasons(self):
        """TV show with exactly 4 seasons should NOT be auto-approved."""
        seasons = [{'seasonNumber': i} for i in range(1, 5)]  # 4 seasons
        item = {'title': 'Some Show', 'seasonRequests': seasons, 'firstAired': '2020-01-01'}
        approved, reason = should_auto_approve(item, 'tv')
        assert approved is False

    def test_tv_exactly_3_seasons_current_year(self):
        """TV show with 3 seasons and current year SHOULD be auto-approved."""
        current_year = datetime.now().year
        seasons = [{'seasonNumber': i} for i in range(1, 4)]  # 3 seasons
        item = {'title': 'New Show', 'seasonRequests': seasons, 'firstAired': f'{current_year}-01-01'}
        approved, reason = should_auto_approve(item, 'tv')
        assert approved is True
        assert str(current_year) in reason

    def test_tv_1_season_current_year(self):
        """TV show with 1 season and current year SHOULD be auto-approved."""
        current_year = datetime.now().year
        seasons = [{'seasonNumber': 1}]
        item = {'title': 'Brand New Show', 'seasonRequests': seasons, 'firstAired': f'{current_year}-06-15'}
        approved, reason = should_auto_approve(item, 'tv')
        assert approved is True

    def test_tv_3_seasons_old_year_no_nzb(self):
        """TV show with 3 seasons but old year and no NZB should NOT be auto-approved."""
        seasons = [{'seasonNumber': i} for i in range(1, 4)]
        item = {'title': 'Old Show', 'seasonRequests': seasons, 'firstAired': '2015-01-01'}
        approved, reason = should_auto_approve(item, 'tv')
        assert approved is False

    def test_tv_15_seasons_current_year(self):
        """TV show with 15 seasons should NOT be auto-approved even if current year."""
        current_year = datetime.now().year
        seasons = [{'seasonNumber': i} for i in range(1, 16)]  # 15 seasons
        item = {'title': 'Long Running Show', 'seasonRequests': seasons, 'firstAired': f'{current_year}-01-01'}
        approved, reason = should_auto_approve(item, 'tv')
        assert approved is False

    def test_tv_season_requests_not_a_list(self):
        """TV show with non-list seasonRequests should NOT be auto-approved."""
        item = {'title': 'Some Show', 'seasonRequests': 'invalid', 'firstAired': '2020-01-01'}
        approved, reason = should_auto_approve(item, 'tv')
        assert approved is False


class TestMovieAutoApprove:
    """Ensure movie auto-approve is unaffected by season checks."""

    def test_movie_current_year(self):
        """Movie from current year SHOULD be auto-approved (no season check)."""
        current_year = datetime.now().year
        item = {'title': 'New Movie', 'releaseDate': f'{current_year}-06-15'}
        approved, reason = should_auto_approve(item, 'movie')
        assert approved is True

    def test_movie_old_year_no_nzb(self):
        """Movie from old year with no NZB should NOT be auto-approved."""
        item = {'title': 'Old Movie', 'releaseDate': '2015-06-15'}
        approved, reason = should_auto_approve(item, 'movie')
        assert approved is False

    def test_movie_no_season_requests_needed(self):
        """Movie should not need seasonRequests at all."""
        current_year = datetime.now().year
        item = {'title': 'Movie', 'releaseDate': f'{current_year}-01-01'}
        # No seasonRequests key - should still work for movies
        approved, reason = should_auto_approve(item, 'movie')
        assert approved is True
