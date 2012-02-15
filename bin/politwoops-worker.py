#!/usr/bin/env python
# encoding: utf-8
"""
politwoops-worker.py

Created by Breyten Ernsting on 2010-05-30.
Copyright (c) 2010 __MyCompanyName__. All rights reserved.
"""

import sys
import getopt
import ConfigParser
import MySQLdb

from time import sleep

import anyjson
import beanstalkc

import socket
# disable buffering
socket._fileobject.default_bufsize = 0

import httplib
httplib.HTTPConnection.debuglevel = 1

import urllib
import urllib2

# external libs
sys.path.insert(0, './lib')

import tweetsclient

import politwoops

from stathat import StatHat

help_message = '''
The help message goes here.
'''

# used for screenshot capturing and archiving
import subprocess
import hashlib
from boto.s3.connection import S3Connection
from boto.s3.key import Key


class Usage(Exception):
    def __init__(self, msg):
        self.msg = msg


class DeletedTweetsWorker:
    def __init__(self, verbose, output, images):
        self.verbose = verbose
        self.output = output
        self.images = images
        self.get_config()
    
    def _debug(self, msg):
        if self.verbose:
            print >>sys.stderr, msg

    def get_database(self):
        self._debug("Making DB connection")
        conn = MySQLdb.connect(
            host=self.config.get('database', 'host'),
            port=int(self.config.get('database', 'port')),
            db=self.config.get('database', 'database'),
            user=self.config.get('database', 'username'),
            passwd=self.config.get('database', 'password'),
            charset="utf8",
            use_unicode=True
        )
        conn.autocommit(True) # needed if you're using InnoDB
        conn.cursor().execute('SET NAMES UTF8')
        return conn
    
    def get_beanstalk(self):
        tube = self.config.get('beanstalk', 'tube')
        self._debug("Initiating beanstalk connection for tube %s ..." % (tube))
        beanstalk = politwoops.utils.beanstalk(host=self.config.get('beanstalk', 'host'), port=int(self.config.get('beanstalk', 'port')), tube=self.config.get('beanstalk', 'tube'))
        return beanstalk

    def get_config(self):
        self._debug("Reading config ...")
        self.config = tweetsclient.Config().get()

    def get_users(self):
        cursor = self.database.cursor()
        q = "SELECT `twitter_id`, `user_name`, `id` FROM `politicians`"
        cursor.execute(q)
        ids = {}
        politicians = {}
        for t in cursor.fetchall():
            ids[t[0]] = t[2]
            politicians[t[0]] = t[1]
        self._debug("Found ids : ")
        self._debug(ids)
        self._debug("Found politicians : ")
        self._debug(politicians)
        return ids, politicians

    def run(self):
        self.database = self.get_database()
        self.beanstalk = self.get_beanstalk()
        self.users, self.politicians = self.get_users()
        self.stathat = self.get_stathat()
        while True:
            sleep(0.2)
            job = self.beanstalk.reserve(timeout=0)
            if job:
                self.handle_tweet(job.body)
                job.delete()
    
    def get_stathat(self):
        stathat_enabled = (self.config.get('stathat', 'enabled') == 'yes')
        if not stathat_enabled:
            self._debug('Running without stathat ...')
            return
        else:
            self._debug('StatHat ingeration enabled ...')
            return StatHat()
        
    def stathat_add_count(self, stat_name):
        if self.stathat is not None:
            try:
                self.stathat.ez_post_count(self.config.get('stathat', 'email'), stat_name, 1)
            except urllib2.URLError, e:
                pass
            except urllib2.HTTPError, e:
                pass
    
    def handle_tweet(self, job_body):
        tweet = anyjson.deserialize(job_body)
        if tweet.has_key('delete'):
            if tweet['delete']['status']['user_id'] in self.users.keys():
                self.handle_deletion(tweet)
        else:
            if tweet.has_key('user') and (tweet['user']['id'] in self.users.keys()):
                self.handle_new(tweet)

                if self.images and tweet.has_key('entities'):
                    tweet_id = tweet['id']

                    for i, url_entity in enumerate(tweet['entities']['urls']):
                        origin_url = url_entity['expanded_url']
                        filename = "%i-%i.png" % (tweet_id, i)

                        archived_url = self.archive_image(filename, origin_url)
                        self._debug("Uploaded image to %s" % archived_url)
                        self.handle_image(tweet, archived_url)
    
    def handle_deletion(self, tweet):
        self._debug("Deleted tweet %s!\n" % (tweet['delete']['status']['id']))
        cursor = self.database.cursor()
        cursor.execute("""SELECT COUNT(*) FROM `tweets` WHERE `id` = %s""", (tweet['delete']['status']['id'],))
        num_previous = cursor.fetchone()[0]
        if num_previous > 0:
            cursor.execute("""UPDATE `tweets` SET `modified` = NOW(), `deleted` = 1 WHERE id = %s""", (tweet['delete']['status']['id'],))
            self.copy_tweet_to_deleted_table(tweet['delete']['status']['id'])
        else:
            cursor.execute("""REPLACE INTO `tweets` (`id`, `deleted`, `modified`, `created`) VALUES(%s, 1, NOW(), NOW())""", (tweet['delete']['status']['id']))
    
    def handle_new(self, tweet):
        self._debug("New tweet %s from user %s/%s (%s)!" % (tweet['id'], tweet['user']['id'], tweet['user']['screen_name'], tweet['user']['id'] in self.users.keys()))
        self.handle_possible_rename(tweet)
        cursor = self.database.cursor()
        cursor.execute("""SELECT COUNT(*), `deleted` FROM `tweets` WHERE `id` = %s""", (tweet['id'],))
        
        info = cursor.fetchone()
        num_previous = info[0]
        if info[1] is not None:
            was_deleted = (int(info[1]) == 1)
        else:
            was_deleted = False
        # cursor.execute("""SELECT COUNT(*) FROM `tweets`""")
        # total_count = cursor.fetchone()[0]
        # self._debug("Total count in table: %s" % total_count)


        if num_previous > 0:
            cursor.execute("""UPDATE `tweets` SET `user_name` = %s, `politician_id` = %s, `content` = %s, `tweet`=%s, `modified`= NOW() WHERE id = %s""", (tweet['user']['screen_name'], self.users[tweet['user']['id']], tweet['text'], anyjson.serialize(tweet), tweet['id'],))
            self._debug("Updated tweet %s\n" % tweet['id'])
        else:
            #cursor.execute("""DELETE FROM `tweets` WHERE `id` = %s""", (tweet['id'],))
            cursor.execute("""INSERT INTO `tweets` (`id`, `user_name`, `politician_id`, `content`, `created`, `modified`, `tweet`) VALUES(%s, %s, %s, %s, NOW(), NOW(), %s)""", (tweet['id'], tweet['user']['screen_name'], self.users[tweet['user']['id']], tweet['text'], anyjson.serialize(tweet)))
            self._debug("Inserted new tweet %s\n" % tweet['id'])

            
        if was_deleted:
            self._debug('Tweet deleted before it came! (%s)' % tweet['id'])
            self.copy_tweet_to_deleted_table(tweet['id'])
        
        self.stathat_add_count('tweets')
    
    def handle_image(self, tweet, url):
        cursor = self.database.cursor()
        cursor.execute("""INSERT INTO `tweet_images` (`tweet_id`, `url`, `created_at`, `updated_at`) VALUES(%s, %s, NOW(), NOW())""", (tweet['id'], url))
        self._debug("Inserted tweet image into database")
    
    def copy_tweet_to_deleted_table(self, tweet_id):
        cursor = self.database.cursor()
        cursor.execute("""INSERT IGNORE INTO `deleted_tweets` SELECT * FROM `tweets` WHERE `id` = %s AND `content` IS NOT NULL""" % (tweet_id))
        self.stathat_add_count('deleted tweets')
        
    def handle_possible_rename(self, tweet):
        tweet_user_name = tweet['user']['screen_name']
        tweet_user_id = tweet['user']['id']
        current_user_name = self.politicians[tweet_user_id]
        if current_user_name != tweet_user_name:
            self.politicians[tweet_user_id] = tweet_user_name
            cursor= self.database.cursor()
            cursor.execute("""UPDATE `politicians` SET `user_name` = %s WHERE `id` = %s""", (tweet_user_name, self.users[tweet_user_id]))
    
    
    # screenshot capturing/archiving functionality

    def url2png_url(self, url):
        apikey = self.config.get('url2png', 'apikey')
        secret = self.config.get('url2png', 'secret')
        bounds = self.config.get('url2png', 'bounds')

        token = hashlib.md5( "%s+%s" % (secret, url) ).hexdigest()
        return "http://api.url2png.com/v3/%s/%s/%s/%s" % (apikey, token, bounds, url)

    def upload_image(self, tmp_path, dest_filename):
        bucket_name = self.config.get('aws', 'bucket_name')
        access_key = self.config.get('aws', 'access_key')
        secret_access_key = self.config.get('aws', 'secret_access_key')
        url_prefix = self.config.get('aws', 'url_prefix')

        dest_path = '%s/%s' % (url_prefix, dest_filename)

        conn = S3Connection(access_key, secret_access_key)
        bucket = conn.create_bucket(bucket_name)
        key = Key(bucket)
        key.key = dest_path
        key.set_contents_from_filename(tmp_path)
        key.set_acl('public-read')

        return 'http://%s/%s' % (bucket_name, dest_path)

    def archive_image(self, dest_filename, origin_url):
        png_url = self.url2png_url(origin_url)
        tmp_dir = self.config.get('url2png', 'tmp_dir')

        tmp_dest = "%s/%s" % (tmp_dir, dest_filename)
        code = subprocess.call("curl %s --output %s --silent --connect-timeout 30" % (png_url, tmp_dest), shell=True)
        if code != 0:
            return None
        
        return self.upload_image(tmp_dest, dest_filename)

def main(argv=None):
    verbose = False
    images = False
    output = None
    
    if argv is None:
        argv = sys.argv
    try:
        try:
            opts, args = getopt.getopt(argv[1:], "ho:iv", ["help", "output=", "images"])
        except getopt.error, msg:
            raise Usage(msg)
        
        # option processing
        for option, value in opts:
            if option == "-v":
                verbose = True
            if option in ("-h", "--help"):
                raise Usage(help_message)
            if option in ("-o", "--output"):
                output = value
            if option in ("-i", "--images"):
                images = True
    
    except Usage, err:
        print >> sys.stderr, sys.argv[0].split("/")[-1] + ": " + str(err.msg)
        print >> sys.stderr, "\t for help use --help"
        return 2
    
    if verbose:
        print "Starting ..."
    if images:
        print "Going to snap screenshots with url2png..."

    app = DeletedTweetsWorker(verbose, output, images)
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
