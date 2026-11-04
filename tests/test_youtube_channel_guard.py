"""YouTube channel/profile links are never handed to yt-dlp; watch, short and playlist URLs are."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import downloadContent as dc


class YoutubeChannelGuardTests(unittest.TestCase):
    def test_channels_rejected(self):
        for u in ('https://www.youtube.com/@someone', 'https://www.youtube.com/channel/UC123',
                  'https://www.youtube.com/c/name/videos', 'https://www.youtube.com/user/x',
                  'https://www.youtube.com/', 'https://youtu.be/', 'https://www.youtube.com/watch',
                  'https://www.youtube.com/playlist'):
            self.assertTrue(dc._is_youtube_channel_url(u), u)

    def test_videos_playlists_and_other_hosts_allowed(self):
        for u in ('https://www.youtube.com/watch?v=abc', 'https://youtu.be/abc',
                  'https://www.youtube.com/playlist?list=PL1', 'https://www.youtube.com/shorts/abc',
                  'https://example.com/@someone', 'https://www.xvideos.com/video1/x'):
            self.assertFalse(dc._is_youtube_channel_url(u), u)


if __name__ == '__main__':
    unittest.main()
