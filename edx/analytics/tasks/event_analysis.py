"""Analyze events for distribution of values, in anticipation of modification for export."""

from collections import defaultdict
import logging
import re

import luigi
import luigi.date_interval

from edx.analytics.tasks.mapreduce import MultiOutputMapReduceJobTask
from edx.analytics.tasks.pathutil import EventLogSelectionMixin, EventLogSelectionTask
from edx.analytics.tasks.url import url_path_join, ExternalURL
import edx.analytics.tasks.util.opaque_key_util as opaque_key_util
from edx.analytics.tasks.util import eventlog

log = logging.getLogger(__name__)


# Treat as an input_id any key that ends with two numbers, each [0-49], with optional suffixes,
INPUT_ID_PATTERN = r'(?P<input_id>.+_[1234]?\d_[1234]?\d)'
INPUT_ID_REGEX = re.compile(r'^{}(_dynamath|_comment|_choiceinput_.*)?$'.format(INPUT_ID_PATTERN))


class EventAnalysisTask(EventLogSelectionMixin, MultiOutputMapReduceJobTask):
    """
    Group events by course and export them for research purposes.
    """

    output_root = luigi.Parameter(
        config_path={'section': 'event-export-course', 'name': 'output_root'}
    )

    # If defined, specifies the location of a dump of auth_userprofile
    # information, to be used to look for corresponding values in
    auth_user = luigi.Parameter(default=None)

    # Allow for filtering input to specific courses, for development.
    course_id = luigi.Parameter(is_list=True, default=[])

    # Define these so that they have normal values by default, and defining gets
    # the exceptional behavior.

    # Do not output events with implicit event_types.
    exclude_implicit = luigi.BooleanParameter(default=False)

    # Do not output events with explicit event_types.
    exclude_explicit = luigi.BooleanParameter(default=False)

    # Exclude collecting information about properties in context.
    exclude_context = luigi.BooleanParameter(default=False)

    # Exclude collecting information about implicit event_types
    # that are not included in exports.  (Catchy name, no?)
    exclude_excluded = luigi.BooleanParameter(default=False)

    # In order to track whether particular event_types are attested at all,
    # just include the "attested" key to the list of keys found for each
    # event_type.
    include_attested = luigi.BooleanParameter(default=False)

    # Turn off special slugging in event_types (other that course_id),
    # and rely on int/hex/alnum and similar slugging.
    disable_slugging = luigi.BooleanParameter(default=False)

    # Turn on checking for user_id and username.
    check_user = luigi.BooleanParameter(default=False)

    # Data loaded from auth_user.
    auth_user_data = None
    username_map = None

    def requires(self):
        results = {
            'events': EventLogSelectionTask(
                source=self.source,
                interval=self.interval,
                pattern=self.pattern,
                expand_interval=self.expand_interval,
            )
        }
        if self.auth_user is not None:
            results['auth_user'] = ExternalURL(self.auth_user)
        return results

    def requires_hadoop(self):
        # Only pass the input files on to hadoop, not any data file.
        return self.requires().get('events')

    def requires_local(self):
        return self.requires().get('auth_user', [])

    def init_mapper(self):
        auth_user_task = self.requires().get('auth_user')
        if auth_user_task is not None:
            with auth_user_task.output().open('r') as auth_user_file:
                self.auth_user_data = {}
                self.username_map = {}
                for line in auth_user_file:
                    self.incr_counter('Event Analysis', 'Auth-user lines read', 1)
                    # Skip over records that are not part of the dump.
                    # i.e. metadata.
                    if '\x01' not in line:
                        continue
                    fields = line.split('\x01')
                    user_id = fields[0]
                    username = fields[1]
                    if len(fields) >= 8:
                        email = fields[7]
                    else:
                        email = "<missing>"
                        log.error("Unable to parse email for user_id %s username %s", user_id, username)
                    self.auth_user_data[user_id] = {
                        'username': username,
                        'email': email
                    }
                    self.username_map[username] = user_id

    def mapper(self, line):
        event, _date_string = self.get_event_and_date_string(line) or (None, None)
        if event is None:
            return

        try:
            course_id = eventlog.get_course_id(event, from_url=True)
            if course_id is None:
                return
        except UnicodeEncodeError:
            # TODO: push this down into util, or better yet into Opaque keys.
            # Seems to spit up when creating the message when trying to raise
            # InvalidKeyError in get_namespace_plugin (__init__.py, line 234).
            # Remove once fix is in master.
            log.exception("Unable to parse course_id from URL: %s", event)
            return

        if self.course_id and course_id not in self.course_id:
            return

        # if self.auth_user is not None:
        user_info = self.get_user_info_from_event(event)

        # We want to look at event_type, and the various keys in the
        # event's data payload.  And then accumulate values for each
        # key as to what types corresponded to it.
        event_type = event.get('event_type')
        if event_type is None:
            log.error("encountered event with no event_type: %s", event)
            return

        canonical_event_type = canonicalize_event_type(
            event_type,
            self.exclude_implicit,
            self.exclude_explicit,
            self.exclude_excluded,
            self.disable_slugging,
            user_info,
        )

        # Only generate a subset of the output by screening out event_type
        # values.  Use switches to control that.  Switches could include:
        #   * only-implicit or only-explicit events
        #   * only-event or event-and-context parameters.
        if canonical_event_type is None:
            return

        event_source = event.get('event_source')
        if event_source is None:
            log.error("encountered event with no event_source: %s", event)
            return

        key_list = []
        event_data = eventlog.get_event_data(event)
        if event_data is not None:
            key_list.extend(get_key_names(event_data, "event", stopwords=['POST', 'GET'], user_info=user_info))

        if not self.exclude_context:
            context = event.get('context')
            if context is not None:
                key_list.extend(get_key_names(context, "context", user_info=user_info))

        if self.include_attested:
            key_list.append("attested")

        # We may want to remove some context keys that we expect to see all the time,
        # like user_id, course_id, org_id, path.  Other context may be more localized.

        # Return each value with its type.  Other ideas include information about the
        # length of the values, to allow us to get stats on that in the reducer.
        for key in key_list:
            yield key.encode('utf8'), (canonical_event_type.encode('utf8'), event_source.encode('utf8'))

    def get_user_info_from_event(self, event):
        # Start simply, and just get obvious info.  See what it matches.
        user_info = {}
        if not self.check_user:
            return user_info

        username = event.get('username').strip()
        if username is not None:
            key = 'username'
            if username.isdigit() and len(username <= 5):
                key = "username-int{}".format(len(username))
            elif username.isdigit():
                key = "username-int"
            elif len(username) <= 5:
                key = "username-{}".format(len(username))
            user_info[key] = username

        user_id = event.get('context', {}).get('user_id')
        if user_id is not None:
            user_id_str = unicode(user_id)
            key = 'user-id'
            if len(user_id_str) <= 4:
                key = "user-id-{}".format(len(user_id_str))
            user_info[key] = user_id_str

        event_user_id = event.get('event', {}).get('user_id')
        if event_user_id is not None:
            key = 'user-id-event'
            if len(event_user_id) <= 4:
                key = "user-id-event-{}".format(len(event_user_id))
            user_info[key] = event_user_id

        # Add some lookups, in case username and user_id don't match.
        if self.auth_user_data and user_id is not None:
            name_for_user_id = self.auth_user_data.get(user_id, {}).get('username')
            if name_for_user_id is not None:
                user_info['username_from_user_id'] = name_for_user_id

        if self.username_map and username is not None:
            id_for_username = self.username_map.get(username)
            if id_for_username is not None:
                user_info['user_id_from_username'] = id_for_username

        return user_info

    def get_event_time(self, event):
        # Some events may emitted and stored for quite some time before actually being entered into the tracking logs.
        # The primary cause of this is mobile devices that go offline for a significant period of time. They will store
        # events locally and then when connectivity is restored transmit them to the server. We log the time that they
        # were received by the server and use that to batch them into exports since it is much simpler than trying to
        # inject them into past exports. The use of EventAnalysisTask is not incremental, we still use this to be
        # consistent with research exports and to be consistent with date of tracking log from where the event came.
        try:
            return event['context']['received_at']
        except KeyError:
            return super(EventAnalysisTask, self).get_event_time(event)

    def output_path_for_key(self, key):
        filename_safe_key = opaque_key_util.get_filename_safe_course_id(key).lower()
        return url_path_join(self.output_root, '{key}.log'.format(key=filename_safe_key,))

    def multi_output_reducer(self, key, values, output_file):
        # first count the values.
        counts = defaultdict(int)
        for value in values:
            counts[value] += 1

        for value in sorted(counts.keys(), key=lambda x: counts[x], reverse=True):
            event_type, source = value
            try:
                new_value = u"{}|{}|{}|{}".format(key, source, event_type, counts[value])
                output_file.write(new_value.strip())
                output_file.write('\n')
                # WARNING: This line ensures that Hadoop knows that our process is not sitting in an infinite loop.
                # Do not remove it.
                self.incr_counter('Event Analysis', 'Raw Bytes Written', len(new_value) + 1)
            except UnicodeDecodeError:
                # Log, but then just continue
                log.exception("encountered bad character in output: key='%r' source='%r' type='%r'", key, source, event_type)


def get_key_names(obj, prefix, stopwords=None, user_info=None):
    """Get information recursively about an object, including type information."""
    stopwords = [word.lower() for word in stopwords] if stopwords is not None else []
    result = []
    if obj is None:
        # Should this ever happen?
        return result
    elif isinstance(obj, dict):
        if len(obj) == 0:
            new_key = u"{}(emptydict)".format(prefix)
            result.append(new_key)
        for key in obj.keys():
            value = obj.get(key)
            canonical_key = canonicalize_key(key)
            if prefix in [
                    'event.export.recommendations',
                    'event.information.export.recommendations',
                    'event.export.removed_recommendations',
                    'event.information.export.removed_recommendations',
            ]:
                canonical_key = '(url)'
            new_prefix = u"{}.{}".format(prefix, canonical_key)
            if key.lower() in stopwords:
                new_keys = [u"{}(TRIMMED)".format(new_prefix)]
            else:
                new_keys = get_key_names(value, new_prefix, stopwords, user_info)
            result.extend(new_keys)
    elif isinstance(obj, list):
        new_prefix = u"{}[]".format(prefix)
        if len(obj) == 0:
            result.append(new_prefix)
        else:
            # Get the list type from the first object,
            # and assume that it's informative enough.
            # (That is, assume there's no dicts, etc. within.)
            entry = obj[0]
            # entry_type = type(entry).__name__
            # new_key = u"{}[({})]".format(prefix, entry_type)
            # result.append(new_key)
            new_keys = get_key_names(entry, new_prefix, stopwords, user_info)
            result.extend(new_keys)
    else:
        # Check to see if values exactly match some form of user info.
        entry_types = []
        if user_info is not None:
            obj_str = unicode(obj)
            for info_name in user_info.iterkeys():
                user_value = unicode(user_info.get(info_name))
                # First look for exact matches.
                if user_value == obj_str:
                    entry_types.append(info_name)
                # Also look for a quoted version.
                elif user_value == '"{}"'.format(obj_str):
                    entry_types.append("{}-quoted".format(info_name))
                # Also look for containment.
                elif unicode(user_info.get(info_name)) in obj_str:
                    entry_types.append("contains-{}".format(info_name))

        if len(entry_types) == 0:
            entry_types.append(type(obj).__name__)
        for entry_type in entry_types:
            new_key = u"{}({})".format(prefix, entry_type)
            result.append(new_key)

    return result


def canonicalize_key(value_string):
    """Convert a string into a canonical form."""
    # Regular expression to identify input_id values:
    match = INPUT_ID_REGEX.match(value_string)
    if match:
        input_id_string = match.group('input_id')
        value_string = value_string.replace(input_id_string, '(input-id)')
        # TODO: determine whether to just return here.  If there is a number
        # still in the string, then the slugging would rewrite the whole thing,
        # including the slug we just found.  No it wouldn't!  It would
        # be delimited, if it were present.
        # return value_string

    # Look for delimiters in the string, and preserve them.
    delimiter_list = ['_', '.']
    for delimiter in delimiter_list:
        if delimiter in value_string:
            values = value_string.split(delimiter)
            return delimiter.join([get_numeric_slug(value) for value in values])
    return get_numeric_slug(value_string)


def get_numeric_slug(value_string, user_info=None):
    if len(value_string) == 0:
        return ""

    # Check if a particular value matches a user_id or username.
    if user_info is not None:
        val_str = unicode(value_string)
        for info_name in sorted(user_info.iterkeys()):
            if val_str == unicode(user_info.get(info_name)):
                return u"({})".format(info_name)

    # If string contains only digits, then return (int<len>).
    if value_string.isdigit():
        return u"(int{})".format(len(value_string))

    hex_digits = set('0123456789abcdefABCDEF')
    if all(c in hex_digits for c in value_string):
        return u"(hex{})".format(len(value_string))

    # If string contains digits and letters, then return (hash<len>).
    if any(char.isdigit() for char in value_string):
        return u"(alnum{})".format(len(value_string))

    return value_string


def canonicalize_event_type(event_type, exclude_implicit, exclude_explicit, exclude_excluded, disable_slugging, user_info=None):
    # if there is no '/' at the beginning, then the event name is the event type:
    # (This should be true of browser events.)
    if not event_type.startswith('/'):
        if exclude_implicit:
            return None
        else:
            return event_type
    elif exclude_explicit:
        return None

    # Find and stub the course_id, if it is present:
    match = opaque_key_util.COURSE_REGEX.match(event_type)
    if match:
        course_id_string = match.group('course_id')
        event_type = event_type.replace(course_id_string, '(course_id)')

    event_type_values = event_type.split('/')

    if event_type_values[1] == 'courses':

        # Assume that /courses is followed by the course_id (if anything):
        if len(event_type_values) > 3 and event_type_values[2] == '(course_id)':

            if event_type_values[3] == 'xblock':
                if exclude_excluded:
                    return None
                if len(event_type_values) >= 6 and event_type_values[5] in ['handler', 'handler_noauth'] and not disable_slugging:
                    event_type_values[4] = '(xblock-loc)'

            elif event_type_values[3] == 'submission_history':
                # Never attested?
                if len(event_type_values) >= 5 and not disable_slugging:
                    event_type_values = event_type_values[0:3] + ['(maybe_username)', '(block-loc)']

            elif event_type_values[3] == 'jump_to_id':
                # Included.
                if len(event_type_values) == 5 and not disable_slugging:
                    event_type_values[4] = '(block-id)'

            elif event_type_values[3] == 'jump_to':
                if exclude_excluded:
                    return None
                if not disable_slugging:
                    event_type_values = event_type_values[0:3] + ['(block-loc)']

            elif event_type_values[3] == 'courseware':
                # Included.
                if not disable_slugging:
                    event_type_values = event_type_values[0:4] + ['(courseware-loc)']

            elif event_type_values[3] == 'xqueue':
                if exclude_excluded:
                    return None
                # /xqueue/(int)/(block-loc)/score_update or ungraded_response
                # If there's nothing following, then always make it a location.
                last = len(event_type_values) - 1
                if last >= 6 and event_type_values[last] in ['score_update', 'ungraded_response'] and not disable_slugging:
                    event_type_values = event_type_values[0:5] + ['(block-loc)'] + event_type_values[last:]

            elif event_type_values[3] == 'wiki':
                # We want to determine the structure at the end, and then stub the
                # random identifier information in between.
                last = len(event_type_values) - 1
                # Skip a trailing empty slash.
                if len(event_type_values[last]) == 0:
                    last = last - 1
                if event_type_values[last] == 'moment.js':
                    last = last - 1

                # We want to handle /_edit, /_create, /_dir, /_delete, /_history, /_settings, /_deleted, /_preview,
                # and /_revision/change/(int5)/.
                for index in range(4, last + 1):
                    if event_type_values[index].startswith('_'):
                        if exclude_excluded:
                            # Exclude commands entirely.
                            return None
                        last = index - 1
                        break

                if not disable_slugging:
                    for index in range(4, last + 1):
                        event_type_values[index] = '(wikislug)'

            elif event_type_values[3] == 'discussion':
                # Comment and thread id's are pretty regular so far, all hex24.  So no need to write special slugging.
                # So just slug discussion and forum IDs (as these are presumably authored, rather than generated).
                if len(event_type_values) >= 6 and event_type_values[5] == 'threads':
                    event_type_values[4] = '(discussion-id)'
                if len(event_type_values) >= 7 and event_type_values[4] == 'forum' and event_type_values[6] in ['threads', 'inline']:
                    event_type_values[5] = '(forum-id)'

            elif event_type_values[3] == 'pdfbook':
                # just keep /courses/(course_id)/pdfbook/(intX)[/chapter/(intX)/[intX]]
                if exclude_excluded:
                    if len(event_type_values) > 4 and len(event_type_values[4]) > 0 and not event_type_values[4].isdigit():
                        return None
                    if len(event_type_values) > 5 and len(event_type_values[5]) > 0 and event_type_values[5] != 'chapter':
                        return None
                    if len(event_type_values) > 6 and len(event_type_values[6]) > 0 and not event_type_values[6].isdigit():
                        return None
                    if len(event_type_values) > 7 and len(event_type_values[7]) > 0 and not event_type_values[7].isdigit():
                        return None
                    if len(event_type_values) > 8:
                        return None
                    # If we got through to here, then it matches the pattern.

            elif exclude_excluded:
                # Figure out what else should be excluded.  Identify top-level.
                if event_type_values[3] in ['info', 'progress', 'course_wiki', 'about', 'teams']:
                    if len(event_type_values) > 4:
                        return None

                else:
                    return None

        elif exclude_excluded:
            # Included implicit events must begin with /courses/(course-id)
            return None

    elif exclude_excluded:
        # Included implicit events must begin with /courses
        return None

    # Done with canonicalization, so just process and output the result.
    return '/'.join([get_numeric_slug(value, user_info) for value in event_type_values])