import logging
from sqlalchemy.orm import relationship
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.schema import Table, ForeignKey
from flexget import schema
from flexget.event import event
from flexget.entry import Entry
from flexget.plugin import priority, register_parser_option, register_plugin
from flexget.utils.sqlalchemy_utils import table_schema, get_index_by_name
from flexget.utils.tools import console, strip_html
from sqlalchemy import Column, Integer, DateTime, Unicode, Index
from datetime import datetime
from flexget.manager import Session

log = logging.getLogger('archive')

SCHEMA_VER = 0

Base = schema.versioned_base('archive', SCHEMA_VER)

archive_tags_table = Table('archive_entry_tags', Base.metadata,
                           Column('entry_id', Integer, ForeignKey('archive_entry.id')),
                           Column('tag_id', Integer, ForeignKey('archive_tag.id')),
                           Index('ix_archive_tags', 'entry_id', 'tag_id'))

archive_sources_table = Table('archive_entry_sources', Base.metadata,
                              Column('entry_id', Integer, ForeignKey('archive_entry.id')),
                              Column('source_id', Integer, ForeignKey('archive_source.id')),
                              Index('ix_archive_sources', 'entry_id', 'source_id'))


class ArchiveEntry(Base):
    __tablename__ = 'archive_entry'
    __table_args__ = (Index('ix_archive_title_url', 'title', 'url'),)

    id = Column(Integer, primary_key=True)
    title = Column(Unicode, index=True)
    url = Column(Unicode, index=True)
    description = Column(Unicode)
    feed = Column(Unicode) # DEPRECATED, but SQLite does not support drop column
    added = Column(DateTime, index=True)

    tags = relationship("ArchiveTag", secondary=archive_tags_table)
    sources = relationship("ArchiveSource", secondary=archive_sources_table, backref='archive_entries')

    def __init__(self):
        self.added = datetime.now()

    def __str__(self):
        return '<ArchiveEntry(title=%s,url=%s,feed=%s,added=%s)>' %\
               (self.title, self.url, self.feed, self.added.strftime('%Y-%m-%d %H:%M'))


class ArchiveTag(Base):
    __tablename__ = 'archive_tag'

    id = Column(Integer, primary_key=True)
    name = Column(Unicode, index=True)

    def __init__(self, name):
        self.name = name

    def __str__(self):
        return '<ArchiveTag(id=%s,name=%s)>' % (self.id, self.name)


class ArchiveSource(Base):
    __tablename__ = 'archive_source'

    id = Column(Integer, primary_key=True)
    name = Column(Unicode, index=True)

    def __init__(self, name):
        self.name = name

    def __str__(self):
        return '<ArchiveSource(id=%s,name=%s)>' % (self.id, self.name)


def get_source(name, session):
    """
    :param string name: Source name
    :param session: SQLAlchemy session
    :return: ArchiveSource from db or new one
    """
    try:
        return session.query(ArchiveSource).filter(ArchiveSource.name == name).one()
    except NoResultFound:
        source = ArchiveSource(name)
        return source


def get_tag(name, session):
    """
    :param string name: Tag name
    :param session: SQLAlchemy session
    :return: ArchiveTag from db or new one
    """
    try:
        return session.query(ArchiveTag).filter(ArchiveTag.name == name).one()
    except NoResultFound:
        source = ArchiveTag(name)
        return source


@schema.upgrade('archive')
def upgrade(ver, session):
    if ver is None:
        # get rid of old index
        aet = table_schema('archive_entry', session)
        old_index = get_index_by_name(aet, 'archive_feed_title')
        if old_index is not None:
            log.info('Dropping legacy index (may take a while) ...')
            old_index.drop()
            # create new index by title, url
        new_index = get_index_by_name(Base.metadata.tables['archive_entry'], 'ix_archive_title_url')
        if new_index:
            log.info('Creating new index (may take a while) ...')
            new_index.create(bind=session.connection())
        else:
            # maybe removed from the model by later migrations?
            log.error('Unable to create index `ix_archive_title_url`, removed from the model?')
            # TODO: nag about this ?
        # This is safe as long as we don't delete the model completely :)
        # But generally never use Declarative Models in migrate!
        if session.query(ArchiveEntry).first():
            log.critical('----------------------------------------------')
            log.critical('You should run `--archive consolidate` ')
            log.critical('one time when you have time, it may take hours')
            log.critical('----------------------------------------------')
        ver = 0
    return ver


class Archive(object):
    """
    Archives all new items into database where they can be later searched and injected.
    Stores the entries in the state as they are at the exit phase, this way feed cleanup for title
    etc is stored into the database. This may however make injecting them back to the original feed work
    wrongly.
    """

    def validator(self):
        from flexget import validator
        config = validator.factory()
        config.accept('boolean')
        config.accept('list').accept('text')
        return config

    def on_feed_exit(self, feed, config):
        """Add new entries into archive. We use exit phase in case the feed corrects title or url via some plugins."""

        if isinstance(config, bool):
            tag_names = []
        else:
            tag_names = config

        tags = []
        for tag_name in set(tag_names):
            tags.append(get_tag(tag_name, feed.session))

        count = 0
        processed = []
        for entry in feed.entries + feed.rejected + feed.failed:
            # I think entry can be in multiple of those lists .. not sure though!
            if entry in processed:
                continue
            else:
                processed.append(entry)

            ae = feed.session.query(ArchiveEntry).\
                 filter(ArchiveEntry.title == entry['title']).\
                 filter(ArchiveEntry.url == entry['url']).first()
            if ae:
                # add (missing) sources
                source = get_source(feed.name, feed.session)
                if not source in ae.sources:
                    log.debug('Adding `%s` into `%s` sources' % (feed.name, ae))
                    ae.sources.append(source)
                # add (missing) tags
                for tag_name in tag_names:
                    atag = get_tag(tag_name, feed.session)
                    if not atag in ae.tags:
                        log.debug('Adding tag %s into %s' % (tag_name, ae))
                        ae.tags.append(atag)
            else:
                # create new archive entry
                ae = ArchiveEntry()
                ae.title = entry['title']
                ae.url = entry['url']
                if 'description' in entry:
                    ae.description = entry['description']
                ae.feed = feed.name
                ae.sources.append(get_source(feed.name, feed.session))
                if tags:
                    # note, we're extending empty list
                    ae.tags.extend(tags)
                log.debug('Adding `%s` with %i tags to archive' % (ae, len(tags)))
                feed.session.add(ae)
                count += 1
        if count:
            log.verbose('Added %i new entries to archive' % count)

    def on_feed_abort(self, feed, config):
        """
        Archive even on feed abort, except if the abort has happened before session
        was started. Eg. in on_process_start
        """
        if feed.session is not None:
            self.on_feed_exit(feed, config)


class ArchiveInject(object):
    """
    Provides functionality to inject items from archive into feeds
    """

    # ArchiveEntries to be injected
    _inject_entries = []
    _injecting_into_feeds = set()
    _inject_ids = set()
    _immortal = False

    @staticmethod
    def inject(id):
        """
        Add :class:`ArchiveEntry` to be injected on run.

        :param int id: Inject :attr:`ArchiveEntry.id` on next run
        """
        ArchiveInject._inject_ids.add(id)

    @staticmethod
    def inject_immortal(value):
        """
        :param bool value: Inject as immortal or not.
        """
        ArchiveInject._immortal = value

    @property
    def injecting(self):
        return bool(ArchiveInject._inject_ids)

    @event('manager.execute.started')
    def reset(*args, **kwargs):
        log.debug('reset ArchiveInject state')
        ArchiveInject._inject_entries = []
        ArchiveInject._injecting_into_feeds = set()
        ArchiveInject._inject_ids = set()
        ArchiveInject._immortal = False

    @priority(512)
    def on_process_start(self, feed, config):
        if not self.injecting:
            return

        # get the entries to be injected, does it only once
        if not self._inject_entries:
            log.debug('Finding inject content')
            session = Session()
            try:
                for id in ArchiveInject._inject_ids:
                    archive_entry = session.query(ArchiveEntry).filter(ArchiveEntry.id == id).first()

                    # not found
                    if not archive_entry:
                        log.critical('There\'s no archived item with ID `%s`' % id)
                        continue

                    # find if there is no longer any feed within sources
                    for source in archive_entry.sources:
                        if source.name in feed.manager.feeds:
                            break
                    else:
                        log.error('None of sources (%s) exists anymore, cannot inject `%s` from archive!' %\
                                  (', '.join([s.name for s in archive_entry.sources]), archive_entry.title))
                        continue

                    # update list of feeds to be injected
                    for source in archive_entry.sources:
                        ArchiveInject._injecting_into_feeds.add(source.name)

                    self._inject_entries.append(archive_entry)
            finally:
                session.close()

        # if this feed is not going to be injected into, abort it
        if feed.name not in ArchiveInject._injecting_into_feeds:
            log.debug('Not going to inject to %s, aborting & disabling' % feed.name)
            feed.enabled = False
            feed.abort(silent=True)
        else:
            log.debug('Injecting to %s, leaving it enabled' % feed.name)

    @priority(255)
    def on_feed_input(self, feed, config):
        if not self.injecting:
            return

        entries = []

        # disable other inputs
        log.info('Disabling all other inputs in the feed.')
        feed.disable_phase('input')

        for inject_entry in self._inject_entries:
            if feed.name not in [s.name for s in inject_entry.sources]:
                # inject_entry was not meant for this feed, continue to next item
                continue
            log.info('Injecting from archive `%s`' % inject_entry.title)
            entry = Entry(inject_entry.title, inject_entry.url)
            if inject_entry.description:
                entry['description'] = inject_entry.description
            if ArchiveInject._immortal:
                log.debug('Injecting as immortal')
                entry['immortal'] = True
            entry['injected'] = True
            entries.append(entry)

        return entries

    @priority(512)
    def on_feed_filter(self, feed, config):
        if not self.injecting:
            return
        for entry in feed.entries:
            if entry.get('injected', False):
                feed.accept(entry, 'injected')


class UrlrewriteArchive(object):
    """
    Provides capability to rewrite urls from archive or make searches with discover.
    """

    entry_map = {'title': 'title',
                 'url': 'url',
                 'description': 'description'}

    def validator(self):
        from flexget import validator

        root = validator.factory()
        root.accept('boolean')
        root.accept('list')
        return root

    def search(self, query, comparator, config=None):
        """Search plugin API method"""
        # TODO: Implement comparator matching, now just returns ordered by age (newest first)

        session = Session()
        try:
            log.debug('looking for `%s` config: %s' % (query, config))
            entries = []
            for archive_entry in search(session, query, desc=True):
                log.debug('rewrite search result: %s' % archive_entry)
                entry = Entry()
                entry.update_using_map(self.entry_map, archive_entry)
                if entry.isvalid():
                    entries.append(entry)
            log.debug('found %i entries' % len(entries))
            return entries
        finally:
            session.close()


def consolidate():
    """
    Converts previous archive data model to new one.
    """

    session = Session()
    try:
        log.verbose('Checking archive size ...')
        count = session.query(ArchiveEntry).count()
        log.verbose('Found %i items to migrate, this can be aborted with CTRL-C safely.' % count)

        # consolidate old data
        from progressbar import ProgressBar, Percentage, Bar, ETA

        widgets = ['Process - ', ETA(), ' ', Percentage(), ' ', Bar(left='[', right=']')]
        bar = ProgressBar(widgets=widgets, maxval=count).start()

        # id's for duplicates
        duplicates = []

        for index, orig in enumerate(session.query(ArchiveEntry).yield_per(5)):
            bar.update(index)

            # item already processed
            if orig.id in duplicates:
                continue

            # item already migrated
            if orig.sources:
                log.info('Database looks like it has already been consolidated, '
                         'item %s has already sources ...' % orig.title)
                session.rollback()
                return

            # add legacy feed to the sources list
            orig.sources.append(get_source(orig.feed, session))
            # remove feed, deprecated .. well, let's still keep it ..
            #orig.feed = None

            for dupe in session.query(ArchiveEntry).\
                        filter(ArchiveEntry.id != orig.id).\
                        filter(ArchiveEntry.title == orig.title).\
                        filter(ArchiveEntry.url == orig.url).all():
                orig.sources.append(get_source(dupe.feed, session))
                duplicates.append(dupe.id)

        if duplicates:
            log.info('Consolidated %i items, removing duplicates ...' % len(duplicates))
            for id in duplicates:
                session.query(ArchiveEntry).filter(ArchiveEntry.id == id).delete()
        session.commit()
        log.info('Completed! This does NOT need to be ran again.')
    except KeyboardInterrupt:
        session.rollback()
        log.critical('Aborted, no changes saved')
    finally:
        session.close()


def tag_source(source_name, tag_names=None):
    """
    Tags all archived entries within a source with supplied tags

    :param string source_name: Source name
    :param list tag_names: List of tag names to add
    """

    if not tag_names or tag_names is None:
        return

    session = Session()
    try:
        # check that source exists
        source = session.query(ArchiveSource).filter(ArchiveSource.name == source_name).first()
        if not source:
            log.critical('Source `%s` does not exists' % source_name)
            srcs = ', '.join([s.name for s in session.query(ArchiveSource).order_by(ArchiveSource.name)])
            if srcs:
                log.info('Known sources: %s' % srcs)
            return

        # construct tags list
        tags = []
        for tag_name in tag_names:
            tags.append(get_tag(tag_name, session))

        # tag 'em
        log.verbose('Please wait while adding tags %s ...' % (', '.join(tag_names)))
        for a in session.query(ArchiveEntry).\
        filter(ArchiveEntry.sources.any(name=source_name)).yield_per(5):
            a.tags.extend(tags)
    finally:
        session.commit()
        session.close()


# API function, was also used from webui .. needs to be rethinked
def search(session, text, tags=None, sources=None, desc=False):
    """
    Search from the archive.

    :param string text: Search keywords, spaces will be replaced with %
    :param Session session: SQLAlchemy session, should not be closed while iterating results.
    :param list tags: Optional list of acceptable tags
    :param list sources: Optional list of acceptable sources
    :param bool desc: Sort results descending
    :return: ArchiveEntries responding to query
    """
    keyword = unicode(text).replace(' ', '%')
    query = session.query(ArchiveEntry).filter(ArchiveEntry.title.like('%' + keyword + '%'))
    if tags:
        query = query.filter(ArchiveEntry.tags.any(ArchiveTag.name.in_(tags)))
    if sources:
        query = query.filter(ArchiveEntry.sources.any(ArchiveSource.name.in_(sources)))
    if desc:
        query = query.order_by(ArchiveEntry.added.desc())
    else:
        query = query.order_by(ArchiveEntry.added.asc())
    for a in query.yield_per(5):
        yield a


class ArchiveCli(object):
    """
    Commandline interface for the Archive plugin
    """

    options = {}
    ACTIONS = ('consolidate', 'search', 'inject', 'tag-source', 'test')

    @staticmethod
    def optik(option, opt, value, parser):

        def print_usage():
            console('Usage for --archive ACTION args, these are subjected to change in near future.')
            console('')
            console(' consolidate               Migrate old archive data to new model, may take a long time.')
            console(' search [@TAG]s KEYWORDS   Search from the archive.')
            console(' inject ID [ID] [yes]      Inject as accepted from archive by ID\'s. '
                    'If yes is given immortal flag will be used.')
            console(' tag-source SRC TAG [TAG]  Tag all archived items within source with given tag.')
            import sys

            sys.exit(1)

        if not parser.rargs:
            print_usage()

        action = parser.rargs[0].lower()
        if action not in ArchiveCli.ACTIONS:
            print_usage()

        ArchiveCli.options['action'] = action
        if len(parser.rargs[0]) > 1:
            args = []
            for arg in parser.rargs[1:]:
                # next parameter starts from there
                if arg.startswith('-'):
                    break
                args.append(unicode(arg))
            ArchiveCli.options['args'] = args

    @priority(768)
    def on_process_start(self, feed, config):
        options = ArchiveCli.options
        # if --archive was not used
        if not options:
            return

        feed.manager.disable_feeds()

        action = options['action']
        args = options.get('args', [])

        if action == 'tag-source':
            if len(args) < 2:
                console('Too few arguments, needs: SOURCE_NAME TAG [TAG]')
                return
            source_name = args[0]
            tag_names = args[1:]
            tag_source(source_name, tag_names=tag_names)
        elif action == 'inject':
            try:
                self.inject(args)
                feed.manager.enable_feeds()
            except ValueError:
                console('Invalid parameters: %s' % ', '.join(args))
                if ',' in ''.join(args):
                    console('IDs must be separated with space now!')
        elif action == 'consolidate':
            consolidate()
        elif action == 'search':
            tags = []
            for arg in args[:]:
                if arg.startswith('@'):
                    tags.append(arg[1:])
                    args.remove(arg)
            self.search(' '.join(args), tags)
        elif action == 'test':
            pass
        else:
            raise NotImplemented(action)

    def search(self, search_term, tags=None):

        def print_ae(ae):
            diff = datetime.now() - ae.added

            console('ID: %-6s | Title: %s\nAdded: %s (%d days ago)\nURL: %s' %\
                    (ae.id, ae.title, ae.added, diff.days, ae.url))
            source_names = ', '.join([s.name for s in ae.sources])
            tag_names = ', '.join([t.name for t in ae.tags])
            console('Source(s): %s | Tag(s): %s' % (source_names or 'N/A', tag_names or 'N/A'))
            if ae.description:
                console('Description: %s' % strip_html(ae.description))
            console('---')

        session = Session()
        try:
            console('Searching: %s' % search_term)
            if tags:
                console('Tags: %s' % ', '.join(tags))
            console('Please wait ...')
            console('')
            for ae in search(session, search_term, tags):
                print_ae(ae)
        finally:
            session.close()

    def inject(self, args):
        ids = []
        immortal_words = ('true', 'y', 'yes')
        immortal = False
        for arg in args:
            if arg in immortal_words:
                immortal = True
                continue
            ids.append(int(arg))
        map(ArchiveInject.inject, ids)
        ArchiveInject.inject_immortal(immortal)


register_plugin(Archive, 'archive', api_ver=2)
register_plugin(ArchiveInject, 'archive_inject', api_ver=2, builtin=True)
register_plugin(UrlrewriteArchive, 'flexget_archive', groups=['search'])
register_plugin(ArchiveCli, '--archive-cli', builtin=True, api_ver=2)
register_parser_option('--archive', action='callback', callback=ArchiveCli.optik,
                       metavar='ARGS',
                       help='Access [search|inject|tag-source|consolidate] functionalities. '
                            'Without any args display help about those.')
