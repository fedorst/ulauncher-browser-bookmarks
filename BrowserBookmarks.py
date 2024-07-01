import json
import logging
import os

from ulauncher.api.client.EventListener import EventListener
from ulauncher.api.client.Extension import Extension
from ulauncher.api.shared.action.OpenUrlAction import OpenUrlAction
from ulauncher.api.shared.action.RenderResultListAction import \
    RenderResultListAction
from ulauncher.api.shared.event import KeywordQueryEvent, SystemExitEvent, PreferencesEvent, PreferencesUpdateEvent
from ulauncher.api.shared.item.ExtensionResultItem import ExtensionResultItem
from abc import ABC, abstractmethod

from typing import List

import sqlite3
import tempfile
import shutil
import configparser

logging.basicConfig()
logger = logging.getLogger(__name__)

class BookmarksHandler(ABC):
    def __init__(self, name, path, image, max_matches_len):
        super().__init__()
        self.active = True
        self.name = name
        self.path = path
        self.image = image
        self.max_matches_len = max_matches_len
        self.matches_len = 0

    def set_active(self, isActive: bool):
        self.active = isActive

    @staticmethod
    def contains_all_substrings(text, substrings):
        for substring in substrings:
            if substring.lower() not in text.lower():
                return False
        return True

    @abstractmethod
    def get_bookmarks(self, query:str) -> List[ExtensionResultItem]:
        pass

    @abstractmethod
    def close(self) -> None:
        pass

# adapted from https://github.com/KuenzelIT/ulauncher-firefox-bookmarks/tree/master
class FirefoxBookmarksHandler(BookmarksHandler):
    def __init__(self, name, path, image, max_matches_len):
        super().__init__(name, path, image, max_matches_len) # TODO: make it possible to NOT have it
        history_location = self.search_places()
        if history_location is None:
            logger.log(f"History location not found at {path} for browser {name}.")
            return
        temporary_history_location = tempfile.mktemp()
        shutil.copyfile(history_location, temporary_history_location) # because DB is locked while firefox is open
        #   Open Firefox history database
        self.conn = sqlite3.connect(temporary_history_location)
        #   External functions
        self.conn.create_function('hostname', 1 ,self.__getHostname)

    #   Get hostname from url
    def __getHostname(self,str):
        url = str.split('/')
        if len(url)>2:
            return url[2]
        else:
            return 'Unknown'

    @staticmethod
    def get_default_profile_path(profile_config):
        for section in profile_config.sections():
            if profile_config.has_option(section, 'Default'):
                if section.startswith("Install"):
                    return profile_config.get(section, 'Default')
            elif profile_config.getint(section, 'Default') == 1:
                if profile_config.has_option(section, 'Path'):
                    return profile_config.get(section, 'Path')

        # TODO: make isRelative=0 possible
        return profile_config.get("Profile0", "Path") 

    def search_places(self) -> str|None:
        #   Firefox folder path
        firefox_path = os.path.join(os.environ['HOME'], self.path + "/")
        #   Firefox profiles configuration file path
        if not os.path.exists(firefox_path):
            return None
        conf_path = os.path.join(firefox_path,'profiles.ini')
        #   Profile config parse
        profile_config = configparser.RawConfigParser()
        profile_config.read(conf_path)
        prof_path = FirefoxBookmarksHandler.get_default_profile_path(profile_config)
        #   Sqlite db directory path
        sql_path = os.path.join(firefox_path,prof_path)
        #   Sqlite db path
        return os.path.join(sql_path,'places.sqlite')

    def fetch_rows(self, query: str):
        # depth 1
        order_by = "A.lastModified DESC" if query == "" else f'instr(LOWER(A.title), LOWER("{query}")) ASC, A.lastModified DESC'
        sql_query = f'''
        SELECT 
            A.title, 
            url, 
            CASE
                WHEN p.title = 'toolbar' THEN A.title
                ELSE COALESCE(p.title || '/', '') || A.title
            END AS full_title
        FROM moz_bookmarks AS A
        LEFT JOIN moz_bookmarks AS p
            ON A.parent = p.id AND p.type = 2
        JOIN moz_places AS B 
            ON(A.fk = B.id)
        WHERE full_title LIKE "%{query}%"
        ORDER BY {order_by}
        LIMIT {self.max_matches_len}
        '''

        cursor = self.conn.cursor()
        logger.debug(f"{sql_query=}")
        cursor.execute(sql_query)
        rows = cursor.fetchall()
        return rows

    def close(self):
        if self.conn is not None:
            self.conn.close()

    def get_bookmarks(self, query: str) -> List[ExtensionResultItem]:
        if query is None:
            query = ''
        items = []
        if self.conn is None:
            return items
        if not self.active:
            return items
        rows = self.fetch_rows(query)
        for link in rows:
            full_title = link[2]
            if full_title.startswith("toolbar/"):
                full_title = full_title[len("toolbar/"):]
            # TODO: favicon of the website
            #icons are found in table moz_favicons .data and .mime_type
            title = link[0]
            url = link[1]
            items.append(ExtensionResultItem(icon=self.image,
                                            name=full_title,
                                            description=url,
                                            on_enter=OpenUrlAction(url)))
        return items

class ChromiumBookmarksHandler(BookmarksHandler):
    def __init__(self, name, path, image, max_matches_len):
        super().__init__(name, path, image, max_matches_len)
        self.bookmark_paths = self.get_bookmark_paths()
    
    def get_bookmark_paths(self) -> List[str]:
        f = os.popen(f'find $HOME/{self.path} | grep Bookmarks')
        res = f.read().split('\n')
        res_lst = []
        if len(res) == 0:
            logger.info(f'Path to the {self.name} Bookmarks was not found')
            return res_lst
        for one_path in res:
            if one_path.endswith('Bookmarks'):
                res_lst.append(one_path)
        return res_lst

    def find_rec(self, bookmark_entry, query, matches):
        if self.matches_len >= self.max_matches_len:
            return

        if bookmark_entry['type'] == 'folder':
            for child_bookmark_entry in bookmark_entry['children']:
                self.find_rec(child_bookmark_entry, query, matches)
        else:
            sub_queries = query.split(' ')
            bookmark_title = bookmark_entry['name']

            if not BookmarksHandler.contains_all_substrings(bookmark_title, sub_queries):
                return

            matches.append(bookmark_entry)
            self.matches_len += 1

    def get_bookmarks(self, query: str) -> List[ExtensionResultItem]:
        items = []
        if not self.active:
            return items
        self.matches_len = 0

        if query is None:
            query = ''

        logger.debug(f'Finding bookmark entries for {query=}')

        if len(self.bookmark_paths) == 0:
            return []

        for bookmarks_path in self.bookmark_paths:
            matches = []
            with open(bookmarks_path) as data_file:
                data = json.load(data_file)
                self.find_rec(data['roots']['bookmark_bar'], query, matches)
                self.find_rec(data['roots']['synced'], query, matches)
                self.find_rec(data['roots']['other'], query, matches)

            for bookmark in matches:
                bookmark_name = bookmark['name'].encode('utf-8')
                bookmark_url = bookmark['url'].encode('utf-8')
                item = ExtensionResultItem(
                    icon=self.image,
                    name='%s' % bookmark_name.decode('utf-8'),
                    description='%s' % bookmark_url.decode('utf-8'),
                    on_enter=OpenUrlAction(bookmark_url.decode('utf-8'))
                )
                items.append(item)
        return items

    def close(self) -> None:
        pass

support_browsers = {
    "chrome": {
        "name": "Google",
        "path": ".config/google-chrome",
        "image": "images/chrome.png",
        "handler": ChromiumBookmarksHandler
    },
    "chromium": {
        "name": "Chromium",
        "path": ".config/chromium",
        "image": "images/chromium.png",
        "handler": ChromiumBookmarksHandler
    },
    "brave": {
        "name": "Brave",
        "path": ".config/BraveSoftware",
        "image": "images/brave.png",
        "handler": ChromiumBookmarksHandler
    },
    "firefox": {
        "name": "Firefox",
        "path": ".mozilla/firefox",
        "image": "images/firefox.png",
        "handler": FirefoxBookmarksHandler
    },
    "snapfirefox": {
        "name": "Firefox (snap)",
        "path": "/snap/firefox/common/.mozilla/firefox",
        "image": "images/firefox.png",
        "handler": FirefoxBookmarksHandler
    }
}

class PreferencesEventListener(EventListener):
    def on_event(self,event,extension):
        for pref in event.preferences:
            extension.set_pref(pref, event.preferences[pref])

class PreferencesUpdateEventListener(EventListener):
    allowed_event_ids = ["search_chrome", "search_chromium", "search_brave", "search_firefox", "firefox_profile"]
    def on_event(self, event, extension):
        if event.id in self.allowed_event_ids:
            extension.set_pref(event.id, event.new_value)

class KeywordQueryEventListener(EventListener):
    def on_event(self, event, extension):
        items = extension.get_final_items(event.get_argument())
        return RenderResultListAction(items)

class SystemExitEventListener(EventListener):
    def on_event(self,event,extension):
        extension.cleanup()

class BrowserBookmarks(Extension):
    max_matches_len = 10

    def get_bookmark_browser_handlers(self) -> List[BookmarksHandler]:
        bookmark_browser_handlers = {}
        for browser_key, browser_obj in support_browsers.items():
            handler: BookmarksHandler = browser_obj["handler"](
                name=browser_obj["name"],
                path=browser_obj["path"],
                image=browser_obj["image"],
                max_matches_len=self.max_matches_len
            )
            bookmark_browser_handlers[browser_key] = handler
        return bookmark_browser_handlers

    def set_pref(self, pref_name, pref_value):
        if pref_name.startswith("search_"):
            targetBrowser = pref_name[len("search_"):]
            targetValue = (pref_value == "yes")
            self.bookmark_browser_handlers[targetBrowser].set_active(targetValue)
        else:
            if pref_name == "firefox_profile":
                pass # TODO: implement

    def __init__(self):
        super(BrowserBookmarks, self).__init__()
        self.bookmark_browser_handlers = self.get_bookmark_browser_handlers()
        self.subscribe(PreferencesEvent, PreferencesEventListener())
        self.subscribe(PreferencesUpdateEvent,PreferencesUpdateEventListener())
        self.subscribe(KeywordQueryEvent, KeywordQueryEventListener())
        self.subscribe(SystemExitEvent,SystemExitEventListener())

    def cleanup(self):
        for browser_key, handler in self.bookmark_browser_handlers.items():
            handler.close()

    def get_final_items(self, query: str):
        final_items = []
        for browser_key, handler in self.bookmark_browser_handlers.items():
            final_items += handler.get_bookmarks(query)
        # TODO: reorder and limit
        return final_items[:self.max_matches_len]

