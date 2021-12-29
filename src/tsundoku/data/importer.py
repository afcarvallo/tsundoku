import logging
import os
import re
import zlib
from itertools import chain
from multiprocessing.pool import ThreadPool
from pathlib import Path

import ahocorasick
import dask
import ftfy
import numpy as np
import pandas as pd
import pytz
import toml
from cytoolz import pluck
from lru import LRU

from tsundoku.features.re import build_re_from_files
from tsundoku.features.text import tokenize
from tsundoku.helpers import read_list


class TweetImporter(object):
    def __init__(self, config_file):
        self.config_file = config_file
        self.config = None
        self.logger = logging.getLogger(__name__)

        with open(self.config_file, "rt") as f:
            self.config = toml.load(f)["project"]

        logging.info(self.config)

        self.configure_locations()
        self.configure_language()
        self.configure_terms()
        self.configure_tokenizer()

        self.timezone = pytz.timezone(self.config["content"].get("timezone"))
        dask.config.set(
            pool=ThreadPool(int(self.config["environment"].get("n_jobs", 1)))
        )

    def filter_dataframe(self, df):
        flag = pd.notnull(df["id"])

        if not self.location["accept_unknown"]:
            if self.location["patterns"]:
                flag = flag & (
                    df["user.location"].str.contains(self.location["patterns"]) == True
                )

        if self.location["blacklist"]:
            flag = flag & ~(
                df["user.location"].str.contains(self.location["blacklist"]) == True
            )

        candidates = df[flag]
        self.logger.info(f"Location filtering: {len(candidates)} from {len(df)} tweets")

        if len(self.automaton):
            result = []
            for tuple in candidates.itertuples():
                # print(getattr(tuple, 'text'))
                findings = set(pluck(1, self.automaton.iter(getattr(tuple, "text"))))
                # print(findings)

                if self.terms["patterns"] is not None:
                    # we have keywords:
                    result.append(
                        "search-term" in findings and not "rejected-term" in findings
                    )
                else:
                    result.append(not "rejected-term" in findings)

            candidates = candidates[result]

        self.logger.info(f"Keyword filtering: {len(candidates)} from {len(df)} tweets")

        return candidates

    def read_tweet_dataframe(self, filename, encoding="utf-8"):
        df = pd.read_json(filename, encoding=encoding, lines=True)

        if not df.empty:
            return self.filter_dataframe(df)

        return df

    def configure_locations(self):
        location_config = self.config["content"].get("location", {})
        self.location = {}
        self.location["accept_unknown"] = bool(location_config.get("accept_unknown", 1))

        if location_config is not None and "blacklist" in location_config:
            blacklist_files = map(
                lambda x: self.config["path"].get("config") + "/" + x,
                location_config["blacklist"],
            )
            patterns = list(
                map(
                    lambda x: x.split(";")[0],
                    filter(lambda x: x, chain(*map(read_list, blacklist_files))),
                )
            )
            self.location["blacklist"] = re.compile("|".join(patterns), re.IGNORECASE)
        else:
            self.logger.warning("no blacklisted locations used")
            self.location["blacklist"] = None

        if location_config is not None and "gazetteers" in location_config:
            gazetteers = map(
                lambda x: self.config["path"].get("config") + "/" + x,
                location_config["gazetteers"],
            )
            patterns = list(
                map(
                    lambda x: x.split(";")[0],
                    filter(lambda x: x, chain(*map(read_list, gazetteers))),
                )
            )
            self.location["patterns"] = re.compile("|".join(patterns), re.IGNORECASE)
        else:
            self.logger.warning("no location filters used")
            self.location["patterns"] = None

    def configure_terms(self):
        self.automaton = ahocorasick.Automaton()

        term_files = self.config["content"].get("term_files", None)
        self.terms = {}

        if term_files is not None:
            term_files = list(
                map(lambda x: self.config["path"].get("config") + "/" + x, term_files)
            )
            self.terms["patterns"] = []  # build_re_from_files(term_files)
            for filename in term_files:
                terms = read_list(filename)
                self.terms["patterns"].extend(terms)
                for term in terms:
                    self.automaton.add_word(term, "search-term")
                self.logger.info(f"read keywords from {filename}: {terms}")
        else:
            self.logger.warning("no keyword terms used")
            self.terms["patterns"] = None

        blacklist = self.config["content"].get("blacklist_files", None)

        if blacklist is None:
            self.logger.warning("no blacklisted keywords used")
            self.terms["blacklist"] = None
        else:
            blacklist = map(
                lambda x: self.config["path"].get("config") + "/" + x, blacklist
            )
            self.terms["blacklist"] = []  # build_re_from_files(blacklist)

            for filename in blacklist:
                terms = read_list(filename)
                self.terms["blacklist"].extend(terms)
                for term in terms:
                    self.automaton.add_word(term, "rejected-term")
                self.logger.info(f"read blacklisted keywords from {filename}: {terms}")

        blacklist_urls = self.config.get("blacklist_urls", None)

        if blacklist_urls is not None:
            blacklist_urls = map(
                lambda x: self.config["path"].get("config") + "/" + x, blacklist_urls
            )
            self.terms["blacklist_urls"] = build_re_from_files(blacklist_urls)
        else:
            self.logger.warning("no blacklisted URLs")

        self.automaton.make_automaton()

    def configure_language(self):
        self.languages = self.config["content"].get("accepted_lang", None)

    def configure_tokenizer(self):
        dtm_config = self.config["content"].get("user_matrix", {})
        ngram_range = dtm_config.get("ngram_range", None)
        stopwords_file = dtm_config.get("stopwords_file", None)

        if stopwords_file is not None:
            stopwords_file = self.config["path"].get("config") + "/" + stopwords_file
            self.logger.info(f"stopwords file: {stopwords_file}")

        if stopwords_file is None:
            stopwords = set()
            self.logger.info("no stopwords")
        else:
            stopwords = set(read_list(stopwords_file))
            self.logger.info(f"#stopwords: {len(stopwords)}")

        token_cache = LRU(dtm_config.get("lru_size", 50))

        def lru_tokenize(x):
            if x in token_cache:
                return token_cache[x]

            result = tokenize(x, ngram_range=ngram_range, stopwords=stopwords)

            token_cache[x] = result

            return result

        self.tokenize = lru_tokenize

    def data_path(self):
        return Path(self.config["path"].get("data"))

    def import_date(self, date, pattern, source_path, periods=24 * 6, freq="10t"):
        date_str = date
        date = pd.to_datetime(date)

        if type(source_path) == str:
            source_path = Path(source_path)
        elif not isinstance(source_path, Path):
            raise ValueError(
                f"source_path is not a valid object (Path or str needed, got {type(source_path)})"
            )

        if not source_path.exists():
            raise ValueError(f"source_path ({source_path}) is not a valid path")

        self.logger.info(f"Source folder: {source_path}")

        if not source_path.exists():
            raise IOError(f"{source_path} does not exist")

        data_date = self.timezone.localize(date).astimezone(pytz.utc)
        self.logger.info(f"UTC start date: {data_date}")

        task_files = []

        for date in pd.date_range(data_date, periods=periods, freq=freq):
            file_path = source_path / pattern.format(date.strftime("%Y%m%d%H%M"))

            if not file_path.exists():
                self.logger.info(f"{file_path} does not exist")
            else:
                task_files.append(file_path)

        self.logger.info(f"#files to import: {len(task_files)}")

        json_path = self.data_path() / "raw" / "json" / date_str

        self.import_files(task_files, json_path, file_prefix='tweets.partition')

    def _read_file(self, i, filename, target_path, file_prefix=None):
        try:
            df = self.read_tweet_dataframe(filename)
        except zlib.error:
            self.logger.error(f"(#{i}) corrupted file: {filename}")
            return 0

        if not "text" in df or df.empty:
            self.logger.error(f"(#{i}) empty file: {filename}")
            return 0

        if file_prefix is not None:
            target_file = target_path / f"{file_prefix}.{i}.json.gz"
        else:
            target_file = target_path / f"{Path(filename).stem}.{i}.json.gz"

        df["tweet.tokens"] = df["text"].map(self.tokenize)
        df["user.description_tokens"] = df["user.description"].map(self.tokenize)
        df["user.name_tokens"] = df["user.name"].map(self.tokenize)

        self.logger.info(f"(#{i}) read {len(df)} tweets from {filename}")

        df.to_json(
            target_file,
            orient="records",
            force_ascii=False,
            lines=True,
            compression="gzip",
        )
        return len(df)

    def import_files(self, file_names, target_path, file_prefix=None):
        if not target_path.exists():
            target_path.mkdir(parents=True)
            self.logger.info("{} directory created".format(target_path))
        else:
            self.logger.info("{} exists".format(target_path))

        tasks = [
            dask.delayed(self._read_file)(i, f, target_path, file_prefix=file_prefix)
            for i, f in enumerate(file_names)
        ]
        read_tweets = sum(dask.compute(*tasks))
        self.logger.info(f"done! imported {read_tweets} tweets")