from nose.plugins.attrib import attr
from flexget.manager import Session
from flexget.plugins.api_tvdb import lookup_episode
from tests import FlexGetBase


class TestThetvdbLookup(FlexGetBase):

    __yaml__ = """
        presets:
          global:
            thetvdb_lookup: yes
            # Access a tvdb field to cause lazy loading to occur
            set:
              afield: "{{ thetvdb_id }}{{ ep_name }}"
        feeds:
          test:
            mock:
              - {title: 'House.S01E02.HDTV.XViD-FlexGet'}
              - {title: 'Doctor.Who.2005.S02E03.PDTV.XViD-FlexGet'}
            series:
              - House
              - Doctor Who 2005
          test_unknown_series:
            mock:
              - {title: 'Aoeu.Htns.S01E01.htvd'}
            series:
              - Aoeu Htns
          test_mark_expired:
            mock:
              - {title: 'House.S02E02.hdtv'}
            metainfo_series: yes
            accept_all: yes
            disable_builtins: [seen]

    """

    @attr(online=True)
    def test_lookup(self):
        """thetvdb: Test Lookup (ONLINE)"""
        self.execute_feed('test')
        entry = self.feed.find_entry(title='House.S01E02.HDTV.XViD-FlexGet')
        assert entry['ep_name'] == 'Paternity', \
            '%s ep_name should be Paternity' % entry['title']
        assert int(entry['series_runtime']) == 60, \
            'runtime for %s is %s, should be 60' % (entry['title'], entry['series_runtime'])
        assert entry['afield'] == '73255Paternity', 'afield was not set correctly'
        assert self.feed.find_entry(ep_name='School Reunion'), \
            'Failed imdb lookup Doctor Who 2005 S02E03'

    @attr(online=True)
    def test_unknown_series(self):
        # Test an unknown series does not cause any exceptions
        self.execute_feed('test_unknown_series')
        # Make sure it didn't make a false match
        entry = self.feed.find_entry('accepted', title='Aoeu.Htns.S01E01.htvd')
        assert entry.get('thetvdb_id') is None, 'should not have populated tvdb data'

    @attr(online=True)
    def test_mark_expired(self):

        def test_run():
            # Run the feed and check tvdb data was populated.
            self.execute_feed('test_mark_expired')
            entry = self.feed.find_entry(title='House.S02E02.hdtv')
            assert entry['ep_name'] == 'Autopsy'

        # Run the feed once, this populates data from tvdb
        test_run()
        # Run the feed again, this should load the data from cache
        test_run()
        # Manually mark the data as expired, to test cache update
        session = Session()
        ep = lookup_episode(name='House', seasonnum=2, episodenum=2, session=session)
        ep.expired = True
        ep.series.expired = True
        session.commit()
        session.close()
        test_run()


class TestThetvdbFavorites(FlexGetBase):
    """
        Tests thetvdb favorites plugin with a test user at thetvdb.
        Test user info:
        username: flexget
        password: flexget
        Account ID: 80FB8BD0720CA5EC
        Favorites: House, Doctor Who 2005, Penn & Teller: Bullshit, Hawaii Five-0 (2010)
    """

    __yaml__ = """
        feeds:
          test:
            mock:
              - {title: 'House.S01E02.HDTV.XViD-FlexGet'}
              - {title: 'Doctor.Who.2005.S02E03.PDTV.XViD-FlexGet'}
              - {title: 'Lost.S03E02.720p-FlexGet'}
              - {title: 'Penn.and.Teller.Bullshit.S02E02.720p.x264'}
            import_series:
              from:
                thetvdb_favorites:
                  account_id: 80FB8BD0720CA5EC
          test_strip_dates:
            thetvdb_favorites:
              account_id: 80FB8BD0720CA5EC
              strip_dates: yes
    """

    @attr(online=True)
    def test_favorites(self):
        """thetvdb: Test favorites (ONLINE)"""
        self.execute_feed('test')
        assert self.feed.find_entry('accepted', title='House.S01E02.HDTV.XViD-FlexGet'), \
            'series House should have been accepted'
        assert self.feed.find_entry('accepted', title='Doctor.Who.2005.S02E03.PDTV.XViD-FlexGet'), \
            'series Doctor Who 2005 should have been accepted'
        assert self.feed.find_entry('accepted', title='Penn.and.Teller.Bullshit.S02E02.720p.x264'), \
            'series Penn and Teller Bullshit should have been accepted'
        entry = self.feed.find_entry(title='Lost.S03E02.720p-FlexGet')
        assert entry, 'Entry not found?'
        assert entry not in self.feed.accepted, \
            'series Lost should not have been accepted'

    @attr(online=True)
    def test_strip_date(self):
        self.execute_feed('test_strip_dates')
        assert self.feed.find_entry(title='Hawaii Five-0'), \
            'series Hawaii Five-0 (2010) should have date stripped'
