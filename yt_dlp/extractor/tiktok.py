import itertools
import json
import random
import string
import time

from playwright.sync_api import sync_playwright

from base64 import b64encode
from urllib.parse import urlencode

from Cryptodome.Cipher import AES
from Cryptodome.Util.Padding import pad

from .common import InfoExtractor
from ..compat import compat_urllib_parse_unquote, compat_urllib_parse_urlparse
from ..utils import (
    ExtractorError,
    HEADRequest,
    LazyList,
    UnsupportedError,
    UserNotLive,
    get_element_by_id,
    get_first,
    int_or_none,
    join_nonempty,
    qualities,
    remove_start,
    srt_subtitles_timecode,
    str_or_none,
    traverse_obj,
    try_get,
    url_or_none,
)


class TikTokBaseIE(InfoExtractor):
    _APP_VERSIONS = [('26.1.3', '260103'), ('26.1.2', '260102'), ('26.1.1', '260101'), ('25.6.2', '250602')]
    _WORKING_APP_VERSION = None
    _APP_NAME = 'trill'
    _AID = 1180
    _API_HOSTNAME = 'api16-normal-c-useast1a.tiktokv.com'
    _UPLOADER_URL_FORMAT = 'https://www.tiktok.com/@%s'
    _WEBPAGE_HOST = 'https://www.tiktok.com/'
    QUALITIES = ('360p', '540p', '720p', '1080p')

    @staticmethod
    def _create_url(user_id, video_id):
        return f'https://www.tiktok.com/@{user_id or "_"}/video/{video_id}'

    def _get_sigi_state(self, webpage, display_id):
        return self._parse_json(get_element_by_id(
            'SIGI_STATE|sigi-persisted-data', webpage, escape_value=False), display_id)

    def _call_api_impl(self, ep, query, manifest_app_version, video_id, fatal=True,
                       note='Downloading API JSON', errnote='Unable to download API page'):
        self._set_cookie(self._API_HOSTNAME, 'odin_tt', ''.join(random.choice('0123456789abcdef') for _ in range(160)))
        webpage_cookies = self._get_cookies(self._WEBPAGE_HOST)
        if webpage_cookies.get('sid_tt'):
            self._set_cookie(self._API_HOSTNAME, 'sid_tt', webpage_cookies['sid_tt'].value)
        return self._download_json(
            'https://%s/aweme/v1/%s/' % (self._API_HOSTNAME, ep), video_id=video_id,
            fatal=fatal, note=note, errnote=errnote, headers={
                'User-Agent': f'com.ss.android.ugc.{self._APP_NAME}/{manifest_app_version} (Linux; U; Android 10; en_US; Pixel 4; Build/QQ3A.200805.001; Cronet/58.0.2991.0)',
                'Accept': 'application/json',
            }, query=query)

    def _build_api_query(self, query, app_version, manifest_app_version):
        return {
            **query,
            'version_name': app_version,
            'version_code': manifest_app_version,
            'build_number': app_version,
            'manifest_version_code': manifest_app_version,
            'update_version_code': manifest_app_version,
            'openudid': ''.join(random.choice('0123456789abcdef') for _ in range(16)),
            'uuid': ''.join([random.choice(string.digits) for _ in range(16)]),
            '_rticket': int(time.time() * 1000),
            'ts': int(time.time()),
            'device_brand': 'Google',
            'device_type': 'Pixel 4',
            'device_platform': 'android',
            'resolution': '1080*1920',
            'dpi': 420,
            'os_version': '10',
            'os_api': '29',
            'carrier_region': 'US',
            'sys_region': 'US',
            'region': 'US',
            'app_name': self._APP_NAME,
            'app_language': 'en',
            'language': 'en',
            'timezone_name': 'America/New_York',
            'timezone_offset': '-14400',
            'channel': 'googleplay',
            'ac': 'wifi',
            'mcc_mnc': '310260',
            'is_my_cn': 0,
            'aid': self._AID,
            'ssmix': 'a',
            'as': 'a1qwert123',
            'cp': 'cbfhckdckkde1',
        }

    def _call_api(self, ep, query, video_id, fatal=True,
                  note='Downloading API JSON', errnote='Unable to download API page'):
        if not self._WORKING_APP_VERSION:
            app_version = self._configuration_arg('app_version', [''], ie_key=TikTokIE.ie_key())[0]
            manifest_app_version = self._configuration_arg('manifest_app_version', [''], ie_key=TikTokIE.ie_key())[0]
            if app_version and manifest_app_version:
                self._WORKING_APP_VERSION = (app_version, manifest_app_version)
                self.write_debug('Imported app version combo from extractor arguments')
            elif app_version or manifest_app_version:
                self.report_warning('Only one of the two required version params are passed as extractor arguments', only_once=True)

        if self._WORKING_APP_VERSION:
            app_version, manifest_app_version = self._WORKING_APP_VERSION
            real_query = self._build_api_query(query, app_version, manifest_app_version)
            return self._call_api_impl(ep, real_query, manifest_app_version, video_id, fatal, note, errnote)

        for count, (app_version, manifest_app_version) in enumerate(self._APP_VERSIONS, start=1):
            real_query = self._build_api_query(query, app_version, manifest_app_version)
            try:
                res = self._call_api_impl(ep, real_query, manifest_app_version, video_id, fatal, note, errnote)
                self._WORKING_APP_VERSION = (app_version, manifest_app_version)
                return res
            except ExtractorError as e:
                if isinstance(e.cause, json.JSONDecodeError) and e.cause.pos == 0:
                    if count == len(self._APP_VERSIONS):
                        if fatal:
                            raise e
                        else:
                            self.report_warning(str(e.cause or e.msg))
                            return
                    self.report_warning('%s. Retrying... (attempt %s of %s)' % (str(e.cause or e.msg), count, len(self._APP_VERSIONS)))
                    continue
                raise e

    def _extract_aweme_app(self, aweme_id):
        feed_list = self._call_api(
            'feed', {'aweme_id': aweme_id}, aweme_id, note='Downloading video feed',
            errnote='Unable to download video feed').get('aweme_list') or []
        aweme_detail = next((aweme for aweme in feed_list if str(aweme.get('aweme_id')) == aweme_id), None)
        if not aweme_detail:
            raise ExtractorError('Unable to find video in feed', video_id=aweme_id)
        return self._parse_aweme_video_app(aweme_detail)

    def _get_subtitles(self, aweme_detail, aweme_id):
        # TODO: Extract text positioning info
        subtitles = {}
        # aweme/detail endpoint subs
        captions_info = traverse_obj(
            aweme_detail, ('interaction_stickers', ..., 'auto_video_caption_info', 'auto_captions', ...), expected_type=dict)
        for caption in captions_info:
            caption_url = traverse_obj(caption, ('url', 'url_list', ...), expected_type=url_or_none, get_all=False)
            if not caption_url:
                continue
            caption_json = self._download_json(
                caption_url, aweme_id, note='Downloading captions', errnote='Unable to download captions', fatal=False)
            if not caption_json:
                continue
            subtitles.setdefault(caption.get('language', 'en'), []).append({
                'ext': 'srt',
                'data': '\n\n'.join(
                    f'{i + 1}\n{srt_subtitles_timecode(line["start_time"] / 1000)} --> {srt_subtitles_timecode(line["end_time"] / 1000)}\n{line["text"]}'
                    for i, line in enumerate(caption_json['utterances']) if line.get('text'))
            })
        # feed endpoint subs
        if not subtitles:
            for caption in traverse_obj(aweme_detail, ('video', 'cla_info', 'caption_infos', ...), expected_type=dict):
                if not caption.get('url'):
                    continue
                subtitles.setdefault(caption.get('lang') or 'en', []).append({
                    'ext': remove_start(caption.get('caption_format'), 'web'),
                    'url': caption['url'],
                })
        # webpage subs
        if not subtitles:
            for caption in traverse_obj(aweme_detail, ('video', 'subtitleInfos', ...), expected_type=dict):
                if not caption.get('Url'):
                    continue
                subtitles.setdefault(caption.get('LanguageCodeName') or 'en', []).append({
                    'ext': remove_start(caption.get('Format'), 'web'),
                    'url': caption['Url'],
                })
        return subtitles

    def _parse_aweme_video_app(self, aweme_detail):
        aweme_id = aweme_detail['aweme_id']
        video_info = aweme_detail['video']

        def parse_url_key(url_key):
            format_id, codec, res, bitrate = self._search_regex(
                r'v[^_]+_(?P<id>(?P<codec>[^_]+)_(?P<res>\d+p)_(?P<bitrate>\d+))', url_key,
                'url key', default=(None, None, None, None), group=('id', 'codec', 'res', 'bitrate'))
            if not format_id:
                return {}, None
            return {
                'format_id': format_id,
                'vcodec': 'h265' if codec == 'bytevc1' else codec,
                'tbr': int_or_none(bitrate, scale=1000) or None,
                'quality': qualities(self.QUALITIES)(res),
            }, res

        known_resolutions = {}

        def extract_addr(addr, add_meta={}):
            parsed_meta, res = parse_url_key(addr.get('url_key', ''))
            if res:
                known_resolutions.setdefault(res, {}).setdefault('height', add_meta.get('height'))
                known_resolutions[res].setdefault('width', add_meta.get('width'))
                parsed_meta.update(known_resolutions.get(res, {}))
                add_meta.setdefault('height', int_or_none(res[:-1]))
            return [{
                'url': url,
                'filesize': int_or_none(addr.get('data_size')),
                'ext': 'mp4',
                'acodec': 'aac',
                'source_preference': -2 if 'aweme/v1' in url else -1,  # Downloads from API might get blocked
                **add_meta, **parsed_meta,
                'format_note': join_nonempty(
                    add_meta.get('format_note'), '(API)' if 'aweme/v1' in url else None, delim=' ')
            } for url in addr.get('url_list') or []]

        # Hack: Add direct video links first to prioritize them when removing duplicate formats
        formats = []
        if video_info.get('play_addr'):
            formats.extend(extract_addr(video_info['play_addr'], {
                'format_id': 'play_addr',
                'format_note': 'Direct video',
                'vcodec': 'h265' if traverse_obj(
                    video_info, 'is_bytevc1', 'is_h265') else 'h264',  # TODO: Check for "direct iOS" videos, like https://www.tiktok.com/@cookierun_dev/video/7039716639834656002
                'width': video_info.get('width'),
                'height': video_info.get('height'),
            }))
        if video_info.get('download_addr'):
            formats.extend(extract_addr(video_info['download_addr'], {
                'format_id': 'download_addr',
                'format_note': 'Download video%s' % (', watermarked' if video_info.get('has_watermark') else ''),
                'vcodec': 'h264',
                'width': video_info.get('width'),
                'height': video_info.get('height'),
                'preference': -2 if video_info.get('has_watermark') else -1,
            }))
        if video_info.get('play_addr_h264'):
            formats.extend(extract_addr(video_info['play_addr_h264'], {
                'format_id': 'play_addr_h264',
                'format_note': 'Direct video',
                'vcodec': 'h264',
            }))
        if video_info.get('play_addr_bytevc1'):
            formats.extend(extract_addr(video_info['play_addr_bytevc1'], {
                'format_id': 'play_addr_bytevc1',
                'format_note': 'Direct video',
                'vcodec': 'h265',
            }))

        for bitrate in video_info.get('bit_rate', []):
            if bitrate.get('play_addr'):
                formats.extend(extract_addr(bitrate['play_addr'], {
                    'format_id': bitrate.get('gear_name'),
                    'format_note': 'Playback video',
                    'tbr': try_get(bitrate, lambda x: x['bit_rate'] / 1000),
                    'vcodec': 'h265' if traverse_obj(
                        bitrate, 'is_bytevc1', 'is_h265') else 'h264',
                    'fps': bitrate.get('FPS'),
                }))

        self._remove_duplicate_formats(formats)
        auth_cookie = self._get_cookies(self._WEBPAGE_HOST).get('sid_tt')
        if auth_cookie:
            for f in formats:
                self._set_cookie(compat_urllib_parse_urlparse(f['url']).hostname, 'sid_tt', auth_cookie.value)

        thumbnails = []
        for cover_id in ('cover', 'ai_dynamic_cover', 'animated_cover', 'ai_dynamic_cover_bak',
                         'origin_cover', 'dynamic_cover'):
            cover = video_info.get(cover_id)
            if cover:
                for cover_url in cover['url_list']:
                    thumbnails.append({
                        'id': cover_id,
                        'url': cover_url,
                    })

        stats_info = aweme_detail.get('statistics', {})
        author_info = aweme_detail.get('author', {})
        music_info = aweme_detail.get('music', {})
        user_url = self._UPLOADER_URL_FORMAT % (traverse_obj(author_info,
                                                             'sec_uid', 'id', 'uid', 'unique_id',
                                                             expected_type=str_or_none, get_all=False))
        labels = traverse_obj(aweme_detail, ('hybrid_label', ..., 'text'), expected_type=str, default=[])

        contained_music_track = traverse_obj(
            music_info, ('matched_song', 'title'), ('matched_pgc_sound', 'title'), expected_type=str)
        contained_music_author = traverse_obj(
            music_info, ('matched_song', 'author'), ('matched_pgc_sound', 'author'), 'author', expected_type=str)

        is_generic_og_trackname = music_info.get('is_original_sound') and music_info.get('title') == 'original sound - %s' % music_info.get('owner_handle')
        if is_generic_og_trackname:
            music_track, music_author = contained_music_track or 'original sound', contained_music_author
        else:
            music_track, music_author = music_info.get('title'), music_info.get('author')

        return {
            'id': aweme_id,
            'extractor_key': TikTokIE.ie_key(),
            'extractor': TikTokIE.IE_NAME,
            'webpage_url': self._create_url(author_info.get('uid'), aweme_id),
            'title': aweme_detail.get('desc'),
            'description': aweme_detail.get('desc'),
            'view_count': int_or_none(stats_info.get('play_count')),
            'like_count': int_or_none(stats_info.get('digg_count')),
            'repost_count': int_or_none(stats_info.get('share_count')),
            'comment_count': int_or_none(stats_info.get('comment_count')),
            'uploader': str_or_none(author_info.get('unique_id')),
            'creator': str_or_none(author_info.get('nickname')),
            'uploader_id': str_or_none(author_info.get('uid')),
            'uploader_url': user_url,
            'track': music_track,
            'album': str_or_none(music_info.get('album')) or None,
            'artist': music_author or None,
            'timestamp': int_or_none(aweme_detail.get('create_time')),
            'formats': formats,
            'subtitles': self.extract_subtitles(aweme_detail, aweme_id),
            'thumbnails': thumbnails,
            'duration': int_or_none(traverse_obj(video_info, 'duration', ('download_addr', 'duration')), scale=1000),
            'availability': self._availability(
                is_private='Private' in labels,
                needs_subscription='Friends only' in labels,
                is_unlisted='Followers only' in labels),
            '_format_sort_fields': ('quality', 'codec', 'size', 'br'),
        }

    def _parse_aweme_video_web(self, aweme_detail, webpage_url):
        video_info = aweme_detail['video']
        author_info = traverse_obj(aweme_detail, 'authorInfo', 'author', expected_type=dict, default={})
        music_info = aweme_detail.get('music') or {}
        stats_info = aweme_detail.get('stats') or {}
        user_url = self._UPLOADER_URL_FORMAT % (traverse_obj(author_info,
                                                             'secUid', 'id', 'uid', 'uniqueId',
                                                             expected_type=str_or_none, get_all=False)
                                                or aweme_detail.get('authorSecId'))

        formats = []
        play_url = video_info.get('playAddr')
        width = video_info.get('width')
        height = video_info.get('height')
        if isinstance(play_url, str):
            formats = [{
                'url': self._proto_relative_url(play_url),
                'ext': 'mp4',
                'width': width,
                'height': height,
            }]
        elif isinstance(play_url, list):
            formats = [{
                'url': self._proto_relative_url(url),
                'ext': 'mp4',
                'width': width,
                'height': height,
            } for url in traverse_obj(play_url, (..., 'src'), expected_type=url_or_none, default=[]) if url]

        download_url = url_or_none(video_info.get('downloadAddr')) or traverse_obj(video_info, ('download', 'url'), expected_type=url_or_none)
        if download_url:
            formats.append({
                'format_id': 'download',
                'url': self._proto_relative_url(download_url),
                'ext': 'mp4',
                'width': width,
                'height': height,
            })
        self._remove_duplicate_formats(formats)

        thumbnails = []
        for thumbnail_name in ('thumbnail', 'cover', 'dynamicCover', 'originCover'):
            if aweme_detail.get(thumbnail_name):
                thumbnails = [{
                    'url': self._proto_relative_url(aweme_detail[thumbnail_name]),
                    'width': width,
                    'height': height
                }]

        return {
            'id': traverse_obj(aweme_detail, 'id', 'awemeId', expected_type=str_or_none),
            'title': aweme_detail.get('desc'),
            'duration': try_get(aweme_detail, lambda x: x['video']['duration'], int),
            'view_count': int_or_none(stats_info.get('playCount')),
            'like_count': int_or_none(stats_info.get('diggCount')),
            'repost_count': int_or_none(stats_info.get('shareCount')),
            'comment_count': int_or_none(stats_info.get('commentCount')),
            'timestamp': int_or_none(aweme_detail.get('createTime')),
            'creator': str_or_none(author_info.get('nickname')),
            'uploader': str_or_none(author_info.get('uniqueId') or aweme_detail.get('author')),
            'uploader_id': str_or_none(traverse_obj(author_info, 'id', 'uid', 'authorId')),
            'uploader_url': user_url,
            'track': str_or_none(music_info.get('title')),
            'album': str_or_none(music_info.get('album')) or None,
            'artist': str_or_none(music_info.get('authorName')),
            'formats': formats,
            'thumbnails': thumbnails,
            'description': str_or_none(aweme_detail.get('desc')),
            'http_headers': {
                'Referer': webpage_url
            }
        }
        



class TikTokIE(TikTokBaseIE):
    _VALID_URL = r'https?://www\.tiktok\.com/(?:embed|@(?P<user_id>[\w\.-]+)/video)/(?P<id>\d+)'
    _EMBED_REGEX = [rf'<(?:script|iframe)[^>]+\bsrc=(["\'])(?P<url>{_VALID_URL})']

    _TESTS = [{
        'url': 'https://www.tiktok.com/@leenabhushan/video/6748451240264420610',
        'md5': '736bb7a466c6f0a6afeb597da1e6f5b7',
        'info_dict': {
            'id': '6748451240264420610',
            'ext': 'mp4',
            'title': '#jassmanak #lehanga #leenabhushan',
            'description': '#jassmanak #lehanga #leenabhushan',
            'duration': 13,
            'height': 1024,
            'width': 576,
            'uploader': 'leenabhushan',
            'uploader_id': '6691488002098119685',
            'uploader_url': 'https://www.tiktok.com/@MS4wLjABAAAA_Eb4t1vodM1IuTy_cvp9CY22RAb59xqrO0Xtz9CYQJvgXaDvZxYnZYRzDWhhgJmy',
            'creator': 'facestoriesbyleenabh',
            'thumbnail': r're:^https?://[\w\/\.\-]+(~[\w\-]+\.image)?',
            'upload_date': '20191016',
            'timestamp': 1571246252,
            'view_count': int,
            'like_count': int,
            'repost_count': int,
            'comment_count': int,
            'artist': 'Ysrbeats',
            'album': 'Lehanga',
            'track': 'Lehanga',
        }
    }, {
        'url': 'https://www.tiktok.com/@patroxofficial/video/6742501081818877190?langCountry=en',
        'md5': '6f3cf8cdd9b28cb8363fe0a9a160695b',
        'info_dict': {
            'id': '6742501081818877190',
            'ext': 'mp4',
            'title': 'md5:5e2a23877420bb85ce6521dbee39ba94',
            'description': 'md5:5e2a23877420bb85ce6521dbee39ba94',
            'duration': 27,
            'height': 960,
            'width': 540,
            'uploader': 'patrox',
            'uploader_id': '18702747',
            'uploader_url': 'https://www.tiktok.com/@MS4wLjABAAAAiFnldaILebi5heDoVU6bn4jBWWycX6-9U3xuNPqZ8Ws',
            'creator': 'patroX',
            'thumbnail': r're:^https?://[\w\/\.\-]+(~[\w\-]+\.image)?',
            'upload_date': '20190930',
            'timestamp': 1569860870,
            'view_count': int,
            'like_count': int,
            'repost_count': int,
            'comment_count': int,
            'artist': 'Evan Todd, Jessica Keenan Wynn, Alice Lee, Barrett Wilbert Weed & Jon Eidson',
            'track': 'Big Fun',
        }
    }, {
        # Banned audio, only available on the app
        'url': 'https://www.tiktok.com/@barudakhb_/video/6984138651336838402',
        'info_dict': {
            'id': '6984138651336838402',
            'ext': 'mp4',
            'title': 'Balas @yolaaftwsr hayu yu ? #SquadRandom_ 🔥',
            'description': 'Balas @yolaaftwsr hayu yu ? #SquadRandom_ 🔥',
            'uploader': 'barudakhb_',
            'creator': 'md5:29f238c49bc0c176cb3cef1a9cea9fa6',
            'uploader_id': '6974687867511718913',
            'uploader_url': 'https://www.tiktok.com/@MS4wLjABAAAAbhBwQC-R1iKoix6jDFsF-vBdfx2ABoDjaZrM9fX6arU3w71q3cOWgWuTXn1soZ7d',
            'track': 'Boka Dance',
            'artist': 'md5:29f238c49bc0c176cb3cef1a9cea9fa6',
            'timestamp': 1626121503,
            'duration': 18,
            'thumbnail': r're:^https?://[\w\/\.\-]+(~[\w\-]+\.image)?',
            'upload_date': '20210712',
            'view_count': int,
            'like_count': int,
            'repost_count': int,
            'comment_count': int,
        }
    }, {
        # Sponsored video, only available with feed workaround
        'url': 'https://www.tiktok.com/@MS4wLjABAAAATh8Vewkn0LYM7Fo03iec3qKdeCUOcBIouRk1mkiag6h3o_pQu_dUXvZ2EZlGST7_/video/7042692929109986561',
        'info_dict': {
            'id': '7042692929109986561',
            'ext': 'mp4',
            'title': 'Slap and Run!',
            'description': 'Slap and Run!',
            'uploader': 'user440922249',
            'creator': 'Slap And Run',
            'uploader_id': '7036055384943690754',
            'uploader_url': 'https://www.tiktok.com/@MS4wLjABAAAATh8Vewkn0LYM7Fo03iec3qKdeCUOcBIouRk1mkiag6h3o_pQu_dUXvZ2EZlGST7_',
            'track': 'Promoted Music',
            'timestamp': 1639754738,
            'duration': 30,
            'thumbnail': r're:^https?://[\w\/\.\-]+(~[\w\-]+\.image)?',
            'upload_date': '20211217',
            'view_count': int,
            'like_count': int,
            'repost_count': int,
            'comment_count': int,
        },
        'expected_warnings': ['trying with webpage', 'Unable to find video in feed']
    }, {
        # Video without title and description
        'url': 'https://www.tiktok.com/@pokemonlife22/video/7059698374567611694',
        'info_dict': {
            'id': '7059698374567611694',
            'ext': 'mp4',
            'title': 'TikTok video #7059698374567611694',
            'description': '',
            'uploader': 'pokemonlife22',
            'creator': 'Pokemon',
            'uploader_id': '6820838815978423302',
            'uploader_url': 'https://www.tiktok.com/@MS4wLjABAAAA0tF1nBwQVVMyrGu3CqttkNgM68Do1OXUFuCY0CRQk8fEtSVDj89HqoqvbSTmUP2W',
            'track': 'original sound',
            'timestamp': 1643714123,
            'duration': 6,
            'thumbnail': r're:^https?://[\w\/\.\-]+(~[\w\-]+\.image)?',
            'upload_date': '20220201',
            'artist': 'Pokemon',
            'view_count': int,
            'like_count': int,
            'repost_count': int,
            'comment_count': int,
        },
    }, {
        # hydration JSON is sent in a <script> element
        'url': 'https://www.tiktok.com/@denidil6/video/7065799023130643713',
        'info_dict': {
            'id': '7065799023130643713',
            'ext': 'mp4',
            'title': '#denidil#денидил',
            'description': '#denidil#денидил',
            'uploader': 'denidil6',
            'uploader_id': '7046664115636405250',
            'uploader_url': 'https://www.tiktok.com/@MS4wLjABAAAAsvMSzFdQ4ikl3uR2TEJwMBbB2yZh2Zxwhx-WCo3rbDpAharE3GQCrFuJArI3C8QJ',
            'artist': 'Holocron Music',
            'album': 'Wolf Sounds (1 Hour) Enjoy the Company of the Animal That Is the Majestic King of the Night',
            'track': 'Wolf Sounds (1 Hour) Enjoy the Company of the Animal That Is the Majestic King of the Night',
            'timestamp': 1645134536,
            'duration': 26,
            'upload_date': '20220217',
            'view_count': int,
            'like_count': int,
            'repost_count': int,
            'comment_count': int,
        },
        'skip': 'This video is unavailable',
    }, {
        # Auto-captions available
        'url': 'https://www.tiktok.com/@hankgreen1/video/7047596209028074758',
        'only_matching': True
    }]
        def get_replies_of_tiktok_comment(self, aweme_id, comment_id):
        reply_json = self._download_json(
            f'https://api-h2.tiktokv.com/aweme/v1/comment/list/reply/?comment_id={comment_id}&item_id={aweme_id}&cursor=0&count=20&insert_ids=&top_ids=&channel_id=0', 
            data=b'', fatal=False, note='Checking if comment has any replies...') or {} 
        has_more = traverse_obj(reply_json, ('has_more'))
        commentsnum = len(reply_json['comments'])

            for i in range(has_more) and commentsnum != 0:
                if i == 0:
                    comment_data = reply_json
                    note='Comment downloading completed!'
                else:
                    comment_data = self._download_json(
                        f'https://api-h2.tiktokv.com/aweme/v1/comment/list/reply/?comment_id={comment_id}&item_id={aweme_id}&count=50&insert_ids=&top_ids=&channel_id=0', 
                        data=b'', fatal=False, query={'cursor': i + 50}, note='Downloading replies...') or {}
                for comment in comment_data['comments']:
                    yield {
                        'id': comment.get('cid'), # comment ID
                        'alt_id': comment.get('aweme_id'), # "aweme" id, seems to be tiktok's universal id, we might swap them
                        'text': comment.get('text'),
                        'like_count': comment.get('digg_count'),
                        'timestamp': comment.get('create_time'),
                        'is_pinned': comment.get('author_pin'), # booleen
                        'is_hidden': comment.get('no_show'), # booleen
                        'lang': comment.get('comment_language'), # 2 letter language code: en, jp, fr, etc. shortened to lang as its more common and saves disk space
                        'text_extra': comment.get('text_extra'), # includes hashtags, most likely same format as in video metadata
                        'reply_count': comment.get('reply_comment_total'), 
                        'author_id': comment['user']['uid'], # user id (possibly aweme id)
                        'author': comment['user']['nickname'], # user nickname
                        'author_label': comment.get('label_text'), 
                        'author_handle': comment['user']['unique_id'], # user handle, @ultimatemariofan101 for example without the at symbol
                        'author_thumbnail': comment['user']['avatar_larger']['url_list'][0], 
                        'author_full_info': comment.get('user'),
                    }

        def _get_comments(self, aweme_id):
            # references: https://gist.github.com/theblazehen/25c18eda95165e65fc5159942fb5e4db (uses v1 api), https://github.com/yt-dlp/yt-dlp/issues/5037 (new api documentation)
            comment_json = self._download_json(
                f'https://api-h2.tiktokv.com/aweme/v2/comment/list/?aweme_id={aweme_id}&cursor=0&count=50&forward_page_type=1', 
                data=b'', fatal=False, note='Checking if video has any comments...') or {} 
            has_more = traverse_obj(comment_json, ('has_more'))
            commentsnum = len(comment_json['comments'])

            for i in range(has_more) and commentsnum != 0:
                if i == 0:
                    comment_data = comment_json
                    note='Comment downloading completed!'
                else:
                    comment_data = self._download_json(
                        f'https://api-h2.tiktokv.com/aweme/v2/comment/list/?aweme_id={aweme_id}&count=50&forward_page_type=1', 
                        data=b'', fatal=False, query={'cursor': i + 50}, note='Downloading a page of comments') or {}
                for comment in comment_data['comments']:
                    yield {
                        'id': comment.get('cid'), # comment ID
                        'alt_id': comment.get('aweme_id'), # "aweme" id, seems to be tiktok's universal id, we might swap them
                        'text': comment.get('text'),
                        'like_count': comment.get('digg_count'),
                        'timestamp': comment.get('create_time'),
                        'is_pinned': comment.get('author_pin'), # booleen
                        'is_hidden': comment.get('no_show'), # booleen
                        'lang': comment.get('comment_language'), # 2 letter language code: en, jp, fr, etc
                        'text_extra': comment.get('text_extra'), # includes hashtags, most likely same format as in video metadata
                        'reply_count': comment.get('reply_comment_total'), 
                        'parent': comment.get('reply_id'), # parent comment if
                        'parent_reply': comment.get('reply_to_reply_id'), # exclusive to replies to replies
                        'author_id': comment['user']['uid'], # user id (possibly aweme id)
                        'author': comment['user']['nickname'], # user nickname
                        'author_label': comment.get('label_text'),
                        'author_handle': comment['user']['unique_id'], # user handle, @ultimatemariofan101 for example without the at symbol
                        'author_thumbnail': comment['user']['avatar_larger']['url_list'][0], 
                        'author_full_info': comment.get('user'),
                    }
                    if self._configuration_arg('no_tiktok_replies') is None:
                        for comment in traverse_obj(comments):
                            if comment.get('reply_comment_total') > 0:
                                get_replies_of_tiktok_comment(self, aweme_id, i)


    def _real_extract(self, url):
        video_id, user_id = self._match_valid_url(url).group('id', 'user_id')
        try:
            return self._extract_aweme_app(video_id)
        except ExtractorError as e:
            self.report_warning(f'{e}; trying with webpage')

        url = self._create_url(user_id, video_id)
        webpage = self._download_webpage(url, video_id, headers={'User-Agent': 'User-Agent:Mozilla/5.0'})
        next_data = self._search_nextjs_data(webpage, video_id, default='{}')
        if next_data:
            status = traverse_obj(next_data, ('props', 'pageProps', 'statusCode'), expected_type=int) or 0
            video_data = traverse_obj(next_data, ('props', 'pageProps', 'itemInfo', 'itemStruct'), expected_type=dict)
        else:
            sigi_data = self._get_sigi_state(webpage, video_id)
            status = traverse_obj(sigi_data, ('VideoPage', 'statusCode'), expected_type=int) or 0
            video_data = traverse_obj(sigi_data, ('ItemModule', video_id), expected_type=dict)

        if status == 0:
            return self._parse_aweme_video_web(video_data, url)
        elif status == 10216:
            raise ExtractorError('This video is private', expected=True)
        raise ExtractorError('Video not available', video_id=video_id)


class TikTokUserIE(TikTokIE):
    IE_NAME = 'tiktok:user'
    _VALID_URL = r'https?://(?:www\.)?tiktok\.com/@(?P<id>[\w\.-]+)/?(?:$|[#?])'
    _WORKING = True
    _TESTS = [{
        'url': 'https://tiktok.com/@therock?lang=en',
        'playlist_mincount': 25,
        'info_dict': {
            'id': '6745191554350760966',
            'title': 'therock',
            'thumbnail': r're:https://.+_100x100\.jpeg',
            'signature': str,
            'follower_count': int,
            'verified': True,
            'private': bool,
            'following_count': int,
            'nickname': str,
            'like_count': int
        },
        'expected_warnings': ['Retrying']
    }, {
        'url': 'https://www.tiktok.com/@pokemonlife22',
        'playlist_mincount': 5,
        'info_dict': {
            'id': '6820838815978423302',
            'title': 'pokemonlife22',
            'thumbnail': r're:https://.+_100x100\.jpeg',
            'signature': str,
            'follower_count': int,
            'verified': bool,
            'private': bool,
            'following_count': int,
            'nickname': str,
            'like_count': int
        },
        'expected_warnings': ['Retrying']
    }, {
        'url': 'https://www.tiktok.com/@meme',
        'playlist_mincount': 25,
        'info_dict': {
            'id': '79005827461758976',
            'title': 'meme',
            'thumbnail': r're:https://.+_100x100\.jpeg',
            'signature': str,
            'follower_count': int,
            'verified': True,
            'private': bool,
            'following_count': int,
            'nickname': str,
            'like_count': int
        },
        'expected_warnings': ['Retrying']
    }]

    def _generate_x_tt_params(self, secUid, device_id, cursor):
        payload = {
            'aid': '1988',
            'app_name': 'tiktok_web',
            'channel': 'tiktok_web',
            'device_platform': 'web_pc',
            'device_id': device_id,
            'region': 'US',
            'priority_region': '',
            'os': 'windows',
            'referer': '',
            'root_referer': 'undefined',
            'cookie_enabled': 'true',
            'screen_width': '1920',
            'screen_height': '1080',
            'browser_language': 'en-US',
            'browser_platform': 'Win32',
            'browser_name': 'Mozilla',
            'browser_version': '5.0 (Windows)',
            'browser_online': 'true',
            'verifyFp': 'undefined',
            'app_language': 'en',
            'webcast_language': 'en',
            'tz_name': 'America/Chicago',
            'is_page_visible': 'true',
            'focus_state': 'false',
            'is_fullscreen': 'false',
            'history_len': '7',
            'from_page': 'user',
            'secUid': secUid,
            'count': '30',
            'cursor': cursor,
            'language': 'en',
            'userId': 'undefined',
            'is_encryption': '1'
        }
        # https://github.com/davidteather/TikTok-Api/issues/899#issuecomment-1175439842
        s = urlencode(payload, doseq=True, quote_via=lambda s, *_: s)
        key = "webapp1.0+202106".encode("utf-8")
        cipher = AES.new(key, AES.MODE_CBC, key)
        ct_bytes = cipher.encrypt(pad(s.encode("utf-8"), AES.block_size))
        return b64encode(ct_bytes).decode("utf-8")

    def _video_entries_api(self, user_name, secUid):
        cursor = '0'
        videos = []
        author = []
        max = self._downloader.params.get('playlistend') or -1
        device_id = ''.join([random.choice(string.digits) for _ in range(16)])
        self.write_debug('Launching headless browser')
        with sync_playwright() as p:
            browser = p.firefox.launch()
            page = browser.new_page()
            page.goto('https://tiktok.com', wait_until='load')
            time.sleep(2)  # it just works ok
            for i in itertools.count(1):
                x_tt_params = self._generate_x_tt_params(secUid, device_id, cursor)
                self.to_screen(f'Downloading page {i}')
                self.write_debug(f'x-tt-params: {x_tt_params}')
                data_json = page.evaluate('([x, d]) => fetch(`https://us.tiktok.com/api/post/item_list/?aid=1988&app_language=en&app_name=tiktok_web&browser_language=en-US&browser_name=Mozilla&browser_online=true&browser_platform=Win32&browser_version=5.0%20%28Windows%29&channel=tiktok_web&cookie_enabled=true&device_id=${d}&device_platform=web_pc&focus_state=true&from_page=user&history_len=7&is_fullscreen=false&is_page_visible=true&os=windows&priority_region=&referer=&region=US&screen_height=1080&screen_width=1920`, { headers: { "x-tt-params": x } }).then(res => res.json())', [x_tt_params, device_id])
                for video in data_json.get('itemList', []):
                    video_id = video.get('id', '')
                    if len(videos) == 0:
                        author = video.get('author', [])
                    video_url = f'https://www.tiktok.com/@{user_name}/video/{video_id}'
                    videos.append(self.url_result(video_url, 'TikTok', video_id, str_or_none(video.get('desc'))))
                    if max > -1 and len(videos) >= max:
                        break
                else:
                    if not data_json.get('hasMore'):
                        break
                    cursor = data_json['cursor']
                    continue
                break
            browser.close()
        return author, videos

    def _entries_api(self, videos):
        for video in videos:
            yield {
                **self._try_extract(video['id']),
                'extractor_key': TikTokIE.ie_key(),
                'extractor': 'TikTok',
                'webpage_url': video['url'],
            }

    def _try_extract(self, id):
        try:
            return self._extract_aweme_app(id)
        except ExtractorError as e:
            self.report_warning(e)
            return {}

    def _get_frontity_state(self, webpage, user_name):
        return traverse_obj(
            self._parse_json(self._search_regex(
                r'(?s)<script[^>]+id=[\'"]__FRONTITY_CONNECT_STATE__[\'"][^>]*>([^<]+)</script>',
                webpage, 'frontity data'), 'frontity data'),
            ('source', 'data', f'/embed/@{user_name}'))

    def _extract_secUid(self, aweme_id):
        feed_list = self._call_api('feed', {'aweme_id': aweme_id}, aweme_id,
                                   note='Downloading video feed', errnote='Unable to download video feed').get('aweme_list') or []
        aweme_detail = next((aweme for aweme in feed_list if str(aweme.get('aweme_id')) == aweme_id), None)
        if not aweme_detail:
            raise ExtractorError('Unable to find video in feed', video_id=aweme_id)
        return traverse_obj(aweme_detail, ('author', 'sec_uid'))

    def _real_extract(self, url):
        user_name = self._match_id(url)
        user_info = []
        secUid = ''

        try:
            webpage = self._download_webpage(f'https://www.tiktok.com/embed/@{user_name}', user_name, note='Downloading user embed')
            state = self._get_frontity_state(webpage, user_name)
            user_info = state.get('userInfo')
            latest_video = next((video for video in state.get('videoList') if len(video.get('playAddr')) > 0), None)
            if latest_video:
                latest_video_id = latest_video.get('id')
                secUid = self._extract_secUid(latest_video_id)
        except ExtractorError as e:
            secUid = self._configuration_arg('secuid', [''], ie_key=TikTokIE)[0]
            if len(secUid) == 0:
                raise e
            self.report_warning(f'{e}; secUid supplied, trying anyway')

        author, response = self._video_entries_api(user_name, secUid)
        if author.get('uniqueId', '') == user_name:
            user_info = author
            user_info['avatarThumbUrl'] = user_info['avatarLarger']

        videos = LazyList(response)

        return self.playlist_result(
            self._entries_api(videos),
            user_info.get('id'), user_name,
            nickname=user_info.get('nickname', user_name),
            thumbnail=user_info.get('avatarThumbUrl', ''),
            verified=user_info.get('verified', False),
            follower_count=user_info.get('followerCount', 0),
            following_count=user_info.get('followingCount', 0),
            like_count=user_info.get('heartCount', 0),
            signature=user_info.get('signature', ''),
            private=user_info.get('privateAccount', False)
        )


class TikTokBaseListIE(TikTokBaseIE):  # XXX: Conventionally, base classes should end with BaseIE/InfoExtractor
    def _entries(self, list_id, display_id):
        query = {
            self._QUERY_NAME: list_id,
            'cursor': 0,
            'count': 20,
            'type': 5,
            'device_id': ''.join(random.choice(string.digits) for i in range(19))
        }

        for page in itertools.count(1):
            for retry in self.RetryManager():
                try:
                    post_list = self._call_api(
                        self._API_ENDPOINT, query, display_id, note=f'Downloading video list page {page}',
                        errnote='Unable to download video list')
                except ExtractorError as e:
                    if isinstance(e.cause, json.JSONDecodeError) and e.cause.pos == 0:
                        retry.error = e
                        continue
                    raise
            for video in post_list.get('aweme_list', []):
                yield {
                    **self._parse_aweme_video_app(video),
                    'extractor_key': TikTokIE.ie_key(),
                    'extractor': 'TikTok',
                    'webpage_url': f'https://tiktok.com/@_/video/{video["aweme_id"]}',
                }
            if not post_list.get('has_more'):
                break
            query['cursor'] = post_list['cursor']

    def _real_extract(self, url):
        list_id = self._match_id(url)
        return self.playlist_result(self._entries(list_id, list_id), list_id)


class TikTokSoundIE(TikTokBaseListIE):
    IE_NAME = 'tiktok:sound'
    _VALID_URL = r'https?://(?:www\.)?tiktok\.com/music/[\w\.-]+-(?P<id>[\d]+)[/?#&]?'
    _WORKING = False
    _QUERY_NAME = 'music_id'
    _API_ENDPOINT = 'music/aweme'
    _TESTS = [{
        'url': 'https://www.tiktok.com/music/Build-a-Btch-6956990112127585029?lang=en',
        'playlist_mincount': 100,
        'info_dict': {
            'id': '6956990112127585029'
        },
        'expected_warnings': ['Retrying']
    }, {
        # Actual entries are less than listed video count
        'url': 'https://www.tiktok.com/music/jiefei-soap-remix-7036843036118469381',
        'playlist_mincount': 2182,
        'info_dict': {
            'id': '7036843036118469381'
        },
        'expected_warnings': ['Retrying']
    }]


class TikTokEffectIE(TikTokBaseListIE):
    IE_NAME = 'tiktok:effect'
    _VALID_URL = r'https?://(?:www\.)?tiktok\.com/sticker/[\w\.-]+-(?P<id>[\d]+)[/?#&]?'
    _WORKING = False
    _QUERY_NAME = 'sticker_id'
    _API_ENDPOINT = 'sticker/aweme'
    _TESTS = [{
        'url': 'https://www.tiktok.com/sticker/MATERIAL-GWOOORL-1258156',
        'playlist_mincount': 100,
        'info_dict': {
            'id': '1258156',
        },
        'expected_warnings': ['Retrying']
    }, {
        # Different entries between mobile and web, depending on region
        'url': 'https://www.tiktok.com/sticker/Elf-Friend-479565',
        'only_matching': True
    }]


class TikTokTagIE(TikTokBaseListIE):
    IE_NAME = 'tiktok:tag'
    _VALID_URL = r'https?://(?:www\.)?tiktok\.com/tag/(?P<id>[^/?#&]+)'
    _WORKING = False
    _QUERY_NAME = 'ch_id'
    _API_ENDPOINT = 'challenge/aweme'
    _TESTS = [{
        'url': 'https://tiktok.com/tag/hello2018',
        'playlist_mincount': 39,
        'info_dict': {
            'id': '46294678',
            'title': 'hello2018',
        },
        'expected_warnings': ['Retrying']
    }, {
        'url': 'https://tiktok.com/tag/fypシ?is_copy_url=0&is_from_webapp=v1',
        'only_matching': True
    }]

    def _real_extract(self, url):
        display_id = self._match_id(url)
        webpage = self._download_webpage(url, display_id, headers={
            'User-Agent': 'facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)'
        })
        tag_id = self._html_search_regex(r'snssdk\d*://challenge/detail/(\d+)', webpage, 'tag ID')
        return self.playlist_result(self._entries(tag_id, display_id), tag_id, display_id)


class DouyinIE(TikTokBaseIE):
    _VALID_URL = r'https?://(?:www\.)?douyin\.com/video/(?P<id>[0-9]+)'
    _TESTS = [{
        'url': 'https://www.douyin.com/video/6961737553342991651',
        'md5': 'a97db7e3e67eb57bf40735c022ffa228',
        'info_dict': {
            'id': '6961737553342991651',
            'ext': 'mp4',
            'title': '#杨超越  小小水手带你去远航❤️',
            'description': '#杨超越  小小水手带你去远航❤️',
            'uploader_id': '110403406559',
            'uploader_url': 'https://www.douyin.com/user/MS4wLjABAAAAEKnfa654JAJ_N5lgZDQluwsxmY0lhfmEYNQBBkwGG98',
            'creator': '杨超越',
            'duration': 19782,
            'timestamp': 1620905839,
            'upload_date': '20210513',
            'track': '@杨超越创作的原声',
            'view_count': int,
            'like_count': int,
            'repost_count': int,
            'comment_count': int,
        },
    }, {
        'url': 'https://www.douyin.com/video/6982497745948921092',
        'md5': '34a87ebff3833357733da3fe17e37c0e',
        'info_dict': {
            'id': '6982497745948921092',
            'ext': 'mp4',
            'title': '这个夏日和小羊@杨超越 一起遇见白色幻想',
            'description': '这个夏日和小羊@杨超越 一起遇见白色幻想',
            'uploader_id': '408654318141572',
            'uploader_url': 'https://www.douyin.com/user/MS4wLjABAAAAZJpnglcjW2f_CMVcnqA_6oVBXKWMpH0F8LIHuUu8-lA',
            'creator': '杨超越工作室',
            'duration': 42608,
            'timestamp': 1625739481,
            'upload_date': '20210708',
            'track': '@杨超越工作室创作的原声',
            'view_count': int,
            'like_count': int,
            'repost_count': int,
            'comment_count': int,
        },
    }, {
        'url': 'https://www.douyin.com/video/6953975910773099811',
        'md5': 'dde3302460f19db59c47060ff013b902',
        'info_dict': {
            'id': '6953975910773099811',
            'ext': 'mp4',
            'title': '#一起看海  出现在你的夏日里',
            'description': '#一起看海  出现在你的夏日里',
            'uploader_id': '110403406559',
            'uploader_url': 'https://www.douyin.com/user/MS4wLjABAAAAEKnfa654JAJ_N5lgZDQluwsxmY0lhfmEYNQBBkwGG98',
            'creator': '杨超越',
            'duration': 17228,
            'timestamp': 1619098692,
            'upload_date': '20210422',
            'track': '@杨超越创作的原声',
            'view_count': int,
            'like_count': int,
            'repost_count': int,
            'comment_count': int,
        },
    }, {
        'url': 'https://www.douyin.com/video/6950251282489675042',
        'md5': 'b4db86aec367ef810ddd38b1737d2fed',
        'info_dict': {
            'id': '6950251282489675042',
            'ext': 'mp4',
            'title': '哈哈哈，成功了哈哈哈哈哈哈',
            'uploader': '杨超越',
            'upload_date': '20210412',
            'timestamp': 1618231483,
            'uploader_id': '110403406559',
            'view_count': int,
            'like_count': int,
            'repost_count': int,
            'comment_count': int,
        },
        'skip': 'No longer available',
    }, {
        'url': 'https://www.douyin.com/video/6963263655114722595',
        'md5': 'cf9f11f0ec45d131445ec2f06766e122',
        'info_dict': {
            'id': '6963263655114722595',
            'ext': 'mp4',
            'title': '#哪个爱豆的105度最甜 换个角度看看我哈哈',
            'description': '#哪个爱豆的105度最甜 换个角度看看我哈哈',
            'uploader_id': '110403406559',
            'uploader_url': 'https://www.douyin.com/user/MS4wLjABAAAAEKnfa654JAJ_N5lgZDQluwsxmY0lhfmEYNQBBkwGG98',
            'creator': '杨超越',
            'duration': 15115,
            'timestamp': 1621261163,
            'upload_date': '20210517',
            'track': '@杨超越创作的原声',
            'view_count': int,
            'like_count': int,
            'repost_count': int,
            'comment_count': int,
        },
    }]
    _APP_VERSIONS = [('23.3.0', '230300')]
    _APP_NAME = 'aweme'
    _AID = 1128
    _API_HOSTNAME = 'aweme.snssdk.com'
    _UPLOADER_URL_FORMAT = 'https://www.douyin.com/user/%s'
    _WEBPAGE_HOST = 'https://www.douyin.com/'

    def _real_extract(self, url):
        video_id = self._match_id(url)

        try:
            return self._extract_aweme_app(video_id)
        except ExtractorError as e:
            e.expected = True
            self.to_screen(f'{e}; trying with webpage')

        webpage = self._download_webpage(url, video_id)
        render_data_json = self._search_regex(
            r'<script [^>]*\bid=[\'"]RENDER_DATA[\'"][^>]*>(%7B.+%7D)</script>',
            webpage, 'render data', default=None)
        if not render_data_json:
            # TODO: Run verification challenge code to generate signature cookies
            cookies = self._get_cookies(self._WEBPAGE_HOST)
            expected = not cookies.get('s_v_web_id') or not cookies.get('ttwid')
            raise ExtractorError(
                'Fresh cookies (not necessarily logged in) are needed', expected=expected)

        render_data = self._parse_json(
            render_data_json, video_id, transform_source=compat_urllib_parse_unquote)
        return self._parse_aweme_video_web(get_first(render_data, ('aweme', 'detail')), url)


class TikTokVMIE(InfoExtractor):
    _VALID_URL = r'https?://(?:(?:vm|vt)\.tiktok\.com|(?:www\.)tiktok\.com/t)/(?P<id>\w+)'
    IE_NAME = 'vm.tiktok'

    _TESTS = [{
        'url': 'https://www.tiktok.com/t/ZTRC5xgJp',
        'info_dict': {
            'id': '7170520270497680683',
            'ext': 'mp4',
            'title': 'md5:c64f6152330c2efe98093ccc8597871c',
            'uploader_id': '6687535061741700102',
            'upload_date': '20221127',
            'view_count': int,
            'like_count': int,
            'comment_count': int,
            'uploader_url': 'https://www.tiktok.com/@MS4wLjABAAAAObqu3WCTXxmw2xwZ3iLEHnEecEIw7ks6rxWqOqOhaPja9BI7gqUQnjw8_5FSoDXX',
            'album': 'Wave of Mutilation: Best of Pixies',
            'thumbnail': r're:https://.+\.webp.*',
            'duration': 5,
            'timestamp': 1669516858,
            'repost_count': int,
            'artist': 'Pixies',
            'track': 'Where Is My Mind?',
            'description': 'md5:c64f6152330c2efe98093ccc8597871c',
            'uploader': 'sigmachaddeus',
            'creator': 'SigmaChad',
        },
    }, {
        'url': 'https://vm.tiktok.com/ZSe4FqkKd',
        'only_matching': True,
    }, {
        'url': 'https://vt.tiktok.com/ZSe4FqkKd',
        'only_matching': True,
    }]

    def _real_extract(self, url):
        new_url = self._request_webpage(
            HEADRequest(url), self._match_id(url), headers={'User-Agent': 'facebookexternalhit/1.1'}).geturl()
        if self.suitable(new_url):  # Prevent infinite loop in case redirect fails
            raise UnsupportedError(new_url)
        return self.url_result(new_url)
    
class TikTokLiveIE(InfoExtractor):
    _VALID_URL = r'https?://(?:www\.)?tiktok\.com/@(?P<id>[\w\.-]+)/live'
    IE_NAME = 'tiktok:live'

    _TESTS = [{
        'url': 'https://www.tiktok.com/@iris04201/live',
        'only_matching': True,
    }]

    def _real_extract(self, url):
        uploader = self._match_id(url)
        webpage = self._download_webpage(url, uploader, headers={'User-Agent': 'User-Agent:Mozilla/5.0'})
        room_id = self._html_search_regex(r'snssdk\d*://live\?room_id=(\d+)', webpage, 'room ID', default=None)
        if not room_id:
            raise ExtractorError('The user is not currently live', expected=True)
        video_js_data = self._download_json(
            'https://www.tiktok.com/api/live/detail/', room_id, query={
                'aid': '1988',
                'roomID': room_id,
            })
        # status = 2 if live else 4
        is_live = traverse_obj(video_js_data, ('LiveRoomInfo', 'status'), expected_type=int, default=4) == 2
        if not is_live:
            raise ExtractorError('The user is not currently live', expected=True)
        live_url = traverse_obj(video_js_data, ('LiveRoomInfo', 'liveUrl'), expected_type=url_or_none)
        if not live_url:
            raise ExtractorError('No stream URL found')

        return {
            'id': room_id,
            'title': (traverse_obj(video_js_data, ('LiveRoomInfo', 'title'), expected_type=str)
                      or self._html_search_meta(['og:title', 'twitter:title'], webpage, default='')),
            'uploader': traverse_obj(video_js_data, ('LiveRoomInfo', 'ownerInfo', 'uniqueId')) or uploader,
            'uploader_id': traverse_obj(video_js_data, ('LiveRoomInfo', 'ownerInfo', 'id')),
            'creator': traverse_obj(video_js_data, ('LiveRoomInfo', 'ownerInfo', 'nickname')),
            'concurrent_view_count': traverse_obj(video_js_data, ('LiveRoomInfo', 'liveRoomStats', 'userCount'), expected_type=int),
            'formats': self._extract_m3u8_formats(live_url, room_id, 'mp4', live=is_live),
            'is_live': is_live,
        }

    
