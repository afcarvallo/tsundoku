# -*- coding: utf-8 -*-
import copy
import logging
import os
import sys
from glob import glob
from multiprocessing.pool import ThreadPool
from pathlib import Path

import click
import dask
import dask.dataframe as dd
import graph_tool
import graph_tool.topology
import numpy as np
import pandas as pd
import toml
from aves.models.network import Network
from dotenv import find_dotenv, load_dotenv
from scipy.sparse import dok_matrix, save_npz

from tsundoku.features.helpers import build_elem_to_id, filter_vocabulary
from tsundoku.features.dtm import build_vocabulary, tokens_to_document_term_matrix
from tsundoku.features.tweets import TWEET_DTYPES
from tsundoku.features.urls import DISCARD_URLS, get_domain
from tsundoku.helpers import read_toml, write_json


@click.command()
@click.option("--experiment", type=str, default="full")
@click.option("--overwrite", type=bool, default=False)
def main(experiment, overwrite):
    """Runs data processing scripts to turn raw data from (../raw) into
    cleaned data ready to be analyzed (saved in ../processed).
    """

    experiment_name = experiment
    logger = logging.getLogger(__name__)
    logger.info("making final data set from raw data")

    config = read_toml(Path(os.environ["TSUNDOKU_PROJECT_PATH"]) / "config.toml")[
        "project"
    ]
    logger.info(str(config))
    dask.config.set(pool=ThreadPool(int(config.get("n_jobs", 2))))

    source_path = Path(config["path"]["data"]) / "raw" / "json"
    experiment_file = Path(config["path"]["config"]) / "experiments.toml"

    if not source_path.exists():
        raise FileNotFoundError(source_path)

    if not experiment_file.exists():
        raise FileNotFoundError(experiment_file)

    with open(experiment_file) as f:
        experiment_config = toml.load(f)
        logging.info(f"{experiment_config}")

    experimental_settings = experiment_config["experiments"][experiment_name]
    logging.info(f"Experimental settings: {experimental_settings}")

    source_folders = sorted(
        glob(str(source_path / experimental_settings.get("folder_pattern", "*")))
    )
    logging.info(
        f"{len(source_folders)} folders with data. {source_folders[0]} up to {source_folders[-1]}"
    )

    key_folders = map(os.path.basename, source_folders)

    if experimental_settings.get("folder_start", None) is not None:
        key_folders = filter(
            lambda x: x >= experimental_settings.get("folder_start"), key_folders
        )

    if experimental_settings.get("folder_end", None) is not None:
        key_folders = filter(
            lambda x: x <= experimental_settings.get("folder_end"), key_folders
        )

    key_folders = list(key_folders)
    logging.info(f"{key_folders}")

    # let's go

    data_base = Path(config["path"]["data"]) / "interim"
    processed_path = (
        Path(config["path"]["data"]) / "processed" / experimental_settings.get("key")
    )

    if not processed_path.exists():
        processed_path.mkdir(parents=True)
        logging.info(f"{processed_path} created")
    else:
        logging.info(f"{processed_path} exists")

    data_paths = [data_base / key for key in key_folders]

    elem_type = "user"
    key_column = "user.id"

    # let's rock

    # the full user network
    # user interactions
    for int_name, int_column in (
        ("reply", "in_reply_to_user_id"),
        ("quote", "quote.user.id"),
        ("retweet", "rt.user.id"),
    ):
        interaction_dd = dd_from_paths(
            [d / f"{int_name}_edgelist.json.gz" for d in data_paths]
        )
        group_user_interactions(
            interaction_dd,
            key_column,
            int_column,
            int_name,
            processed_path,
            overwrite=overwrite,
        )

    # users
    count_user_tweets(data_paths, processed_path, overwrite=overwrite)
    group_users(
        data_paths,
        processed_path,
        discussion_only=bool(experimental_settings.get("discussion_only")),
        directed=bool(experimental_settings.get("discussion_directed", False)),
        overwrite=overwrite,
    )

    # matrices
    stopwords_file = Path(config["path"]["config"]) / "stopwords.txt"

    if not stopwords_file.exists():
        stopwords_file = None

    min_freq = experiment_config["thresholds"].get("name_tokens", 50)
    # we read this again to catch changes in biographies and so on
    users_dd = dd_from_paths([d / "unique_users.json.gz" for d in data_paths])
    # this file exists from group_users
    elem_to_id = (
        pd.read_json(processed_path / "user.elem_ids.json.gz", lines=True)
        .set_index("user.id")["row_id"]
        .to_dict()
    )

    build_vocabulary_and_matrix(
        users_dd,
        processed_path,
        elem_type,
        key_column,
        "user.name_tokens",
        elem_to_id,
        min_freq=min_freq,
        stopwords_file=stopwords_file,
        overwrite=overwrite,
    )

    min_freq = experiment_config["thresholds"].get("description_tokens", 50)
    build_vocabulary_and_matrix(
        users_dd,
        processed_path,
        elem_type,
        key_column,
        "user.description_tokens",
        elem_to_id,
        min_freq=min_freq,
        stopwords_file=stopwords_file,
        overwrite=overwrite,
    )

    # user-tweet matrix
    terms_dd = dd_from_paths([d / "tweet_vocabulary.json.gz" for d in data_paths])
    min_freq = experiment_config["thresholds"].get("tweet_tokens", 50)
    build_user_tweets_term_matrix(
        terms_dd,
        processed_path,
        elem_to_id,
        min_freq=min_freq,
        stopwords_file=stopwords_file,
        overwrite=overwrite,
    )

    # user networks
    for int_name, int_column in (
        ("reply", "in_reply_to_user_id"),
        ("quote", "quote.user.id"),
        ("retweet", "rt.user.id"),
    ):
        # interaction_dd = dd_from_paths([d / f'{int_name}_edgelist.json.gz' for d in data_paths])
        # group_user_interactions(interaction_dd, key_column, int_column, int_name, processed_path)
        build_network(
            int_name, int_column, elem_to_id, processed_path, overwrite=overwrite
        )

    # user tweet urls
    urls_dd = dd_from_paths([d / "user_urls.json.gz" for d in data_paths])
    min_freq = experiment_config["thresholds"].get("tweet_domains", 50)
    group_user_urls(
        urls_dd, elem_to_id, processed_path, min_freq=50, overwrite=overwrite
    )

    # user profile domains
    min_freq = experiment_config["thresholds"].get("profile_domains", 10)
    min_freq_tld = experiment_config["thresholds"].get("profile_tlds", 50)
    group_profile_domains(
        users_dd,
        elem_to_id,
        processed_path,
        min_freq=min_freq,
        min_freq_tld=min_freq_tld,
        overwrite=overwrite,
    )


def dd_from_paths(paths, min_size=100):
    valid_paths = list(
        filter(lambda x: os.path.exists(x) and os.stat(x).st_size >= min_size, paths)
    )
    return dd.read_json(valid_paths)


def count_user_tweets(data_paths, destination_path, overwrite=False):
    count_target = destination_path / "user.total_tweets.json.gz"

    if not overwrite and count_target.exists():
        logging.info("total tweet counts were computed! skipping.")
        return

    tweet_dd = (
        dd_from_paths([d / "tweets_per_user.json.gz" for d in data_paths])
        .groupby("user.id")["0"]
        .sum()
        .compute()
        .rename("user.dataset_tweets")
        .sort_values()
    )

    print(tweet_dd.describe())

    tweet_dd.reset_index().to_json(
        count_target, compression="gzip", orient="records", lines=True
    )
    logging.info(f"user tweet counts -> {count_target}")


def group_users(
    data_paths, processed_path, discussion_only=True, directed=False, overwrite=False
):
    target_file = processed_path / "user.unique.json.gz"
    elem_ids_target = processed_path / "user.elem_ids.json.gz"

    if not overwrite and (target_file.exists() and elem_ids_target.exists()):
        logging.info("users were already grouped! skipping.")
        return

    users = (
        dd_from_paths([d / "unique_users.json.gz" for d in data_paths])
        .compute()
        .drop_duplicates(subset="user.id", keep="last")
    )

    if discussion_only:
        logging.info(f"total #users before filtering by discussion: {len(users)}")
        nodes_in_largest = find_nodes_in_discussion(processed_path, directed=directed)
        users = users[users["user.id"].isin(nodes_in_largest.index)]

    logging.info(f"total #users: {len(users)}")

    tweet_count = pd.read_json(
        processed_path / "user.total_tweets.json.gz", lines=True
    ).set_index("user.id")

    users = users.join(tweet_count, on="user.id", how="inner").sort_values(
        "user.dataset_tweets", ascending=False
    )
    logging.info(f"total #users after joining with tweet count: {len(users)}")

    users.to_json(target_file, compression="gzip", orient="records", lines=True)
    logging.info(f"#{len(users)} users -> {target_file}")

    (
        users[["user.id"]]
        .assign(row_id=lambda x: range(len(x)))
        .to_json(elem_ids_target, compression="gzip", orient="records", lines=True)
    )

    logging.info(f"user row ids -> {elem_ids_target}")


def build_vocabulary_and_matrix(
    dask_df,
    destination_path,
    elem_type,
    key_column,
    token_column,
    elem_to_id,
    to_lower=True,
    min_freq=50,
    remove_punctuation=True,
    stopwords_file=None,
    overwrite=False,
):
    vocabulary_target = destination_path / "{}.{}.all.json.gz".format(
        elem_type, token_column.replace(elem_type + ".", "")
    )
    relevant_vocabulary_target = destination_path / "{}.{}.relevant.json.gz".format(
        elem_type, token_column.replace(elem_type + ".", "")
    )
    token_matrix_target = destination_path / "{}.{}.matrix.npz".format(
        elem_type, token_column.replace(elem_type + ".", "")
    )
    relevant_vocabulary = None

    if not overwrite and (
        vocabulary_target.exists() and relevant_vocabulary_target.exists()
    ):
        logging.info("vocabulary was computed! skipping.")
        # relevant_vocabulary = pd.read_json(relevant_vocabulary_target, lines=True)
    else:
        vocabulary = build_vocabulary(dask_df, token_column, to_lower=to_lower)
        vocabulary.reset_index().to_json(
            vocabulary_target, compression="gzip", orient="records", lines=True
        )
        logging.info(f"{elem_type}.{token_column} vocabulary -> {vocabulary_target}")

        relevant_vocabulary = filter_vocabulary(
            vocabulary,
            min_freq=min_freq,
            stopwords_file=stopwords_file,
            remove_punctuation=remove_punctuation,
        )

        logging.info(
            f"{elem_type}.{token_column} relevant vocabulary -> {relevant_vocabulary_target}"
        )
        relevant_vocabulary.reset_index().to_json(
            relevant_vocabulary_target, compression="gzip", orient="records", lines=True
        )

    if not overwrite and token_matrix_target.exists():
        logging.info(f"{elem_type}.{token_column} matrix exists! skipping.")
    else:
        if relevant_vocabulary is None:
            relevant_vocabulary = pd.read_json(
                relevant_vocabulary_target, lines=True
            ).set_index("token")
        token_to_id = relevant_vocabulary["token_id"].to_dict()
        token_to_id = relevant_vocabulary["token_id"].to_dict()

        token_matrices = dask_df.map_partitions(
            lambda df: tokens_to_document_term_matrix(
                df, key_column, token_column, token_to_id, id_to_row=elem_to_id
            ),
            meta=(
                "matrix",
                np.dtype("O"),
            ),
        )
        token_matrix = token_matrices.compute().sum()
        save_npz(token_matrix_target, token_matrix)
        logging.info(f"{elem_type}.{token_column} matrix -> {token_matrix_target}")


def build_user_tweets_term_matrix(
    term_frequencies,
    destination_path,
    elem_to_id,
    min_freq=50,
    stopwords_file=None,
    overwrite=False,
):
    full_vocabulary_target = destination_path / "user.tweet_vocabulary.all.json.gz"
    relevant_full_vocabulary_target = (
        destination_path / "user.tweet_vocabulary.relevant.json.gz"
    )
    tweet_matrix_target = destination_path / "user.tweets.matrix.npz"

    relevant_full_vocabulary = None

    if not overwrite and (
        full_vocabulary_target.exists() and relevant_full_vocabulary_target.exists()
    ):
        logging.info("vocabulary was computed! skipping.")
    else:
        full_vocabulary = term_frequencies.groupby("token")["frequency"].sum().compute()
        full_vocabulary.reset_index().to_json(
            full_vocabulary_target, compression="gzip", orient="records", lines=True
        )
        logging.info(f"user.tweet_tokens vocabulary -> {full_vocabulary_target}")

        relevant_full_vocabulary = filter_vocabulary(
            full_vocabulary.reset_index(),
            min_freq=min_freq,
            stopwords_file=stopwords_file,
            remove_punctuation=True,
        )
        relevant_full_vocabulary.reset_index().to_json(
            relevant_full_vocabulary_target,
            compression="gzip",
            orient="records",
            lines=True,
        )
        logging.info(
            f"user.tweet_tokens relevant vocabulary -> {relevant_full_vocabulary_target}"
        )

    if not overwrite and tweet_matrix_target.exists():
        logging.info(f"user.tweet_tokens matrix exists! skipping.")
    else:
        if relevant_full_vocabulary is None:
            relevant_full_vocabulary = pd.read_json(
                relevant_full_vocabulary_target, lines=True
            ).set_index("token")

        # print(elem_to_id)
        user_tweet_frequency = (
            term_frequencies.pipe(
                lambda x: x[
                    (x["user.id"].isin(elem_to_id.keys()))
                    & (x["token"].isin(relevant_full_vocabulary.index))
                ]
            )
            .groupby(["user.id", "token"])
            .sum()
            .compute()
        )

        logging.info(f"user_tweet_frequency: {user_tweet_frequency.shape}")

        tweet_token_to_id = relevant_full_vocabulary["token_id"].to_dict()
        # print(tweet_token_to_id)

        dtm = dok_matrix(
            (max(elem_to_id.values()) + 1, max(tweet_token_to_id.values()) + 1),
            dtype=np.int64,
        )

        for row in user_tweet_frequency.itertuples():
            if not row.Index[0] in elem_to_id:
                continue
            row_id = elem_to_id[row.Index[0]]
            column_id = tweet_token_to_id[row.Index[1]]
            dtm[row_id, column_id] = getattr(row, "frequency")

        dtm = dtm.tocsr()
        logging.info(f"user.tweet_tokens matrix: {repr(dtm)}")

        save_npz(tweet_matrix_target, dtm)
        logging.info(f"user.tweet_tokens matrix -> {tweet_matrix_target}")


def find_nodes_in_discussion(processed_path, directed=False, overwrite=False):
    layers = pd.DataFrame(columns=["source.id", "target.id"])

    for layer_name in ["retweet", "reply", "quote"]:
        layer = pd.read_json(
            processed_path / f"user.{layer_name}_edges.all.json.gz", lines=True
        )
        layer.columns = ["source.id", "target.id", layer_name]
        layers = layers.merge(layer, how="outer").fillna(0)
        logging.info(f"full network layer {layer_name}: {layers.shape}")

    for layer_name in ["retweet", "reply", "quote"]:
        layers[layer_name] = layers[layer_name].astype(np.int)

    layers["weight"] = layers[["retweet", "reply", "quote"]].sum(axis=1)

    unique_user_ids = set(layers["source.id"].unique()) | set(
        layers["target.id"].unique()
    )
    id_to_node = dict(zip(unique_user_ids, range(len(unique_user_ids))))

    layers["source.node"] = layers["source.id"].map(id_to_node)
    layers["target.node"] = layers["target.id"].map(id_to_node)

    network = Network.from_edgelist(
        layers,
        source="source.node",
        target="target.node",
        weight="weight",
        directed=False,
    )
    layer_graph, layer_weight = network.graph, network.edge_weight

    logging.info(
        f"full network #vertices: {layer_graph.num_vertices()}, #edges: {layer_graph.num_edges()}"
    )
    layer_components, component_histogram = graph_tool.topology.label_components(
        layer_graph, directed=False
    )
    component_histogram = pd.Series(
        component_histogram, name="component_sizes"
    ).to_frame()
    component_histogram.to_csv(
        processed_path / "discussion.component_histogram.csv.gz", compression="gzip"
    )

    node_components = pd.Series(
        layer_components.a, name="component_id", index=unique_user_ids
    ).to_frame()
    node_components.index.name = "user.id"
    largest_id = np.argmax(component_histogram)
    nodes_in_largest = node_components[node_components["component_id"] == largest_id]
    logging.info(f"#nodes in largest component: #{nodes_in_largest.shape[0]}")
    return nodes_in_largest


def build_network(
    name,
    target_column,
    elem_to_id,
    destination_path,
    source_column="user.id",
    overwrite=False,
):
    edges_path = destination_path / f"user.{name}_edges.all.json.gz"
    id_to_node_path = destination_path / f"network.{name}.target_ids.json.gz"
    adjacency_matrix_path = destination_path / f"network.{name}.matrix.npz"

    if not overwrite and (id_to_node_path.exists() and adjacency_matrix_path.exists()):
        logging.info(f"{name} adjacency matrix exists! skipping.")
        return

    edges = pd.read_json(edges_path, lines=True).pipe(
        lambda x: x[x[source_column].isin(elem_to_id.keys())]
    )

    unique_user_ids = set(edges[source_column].unique()) | set(
        edges[target_column].unique()
    )
    id_to_node = copy.deepcopy(elem_to_id)

    non_dataset_users = list(filter(lambda x: not x in id_to_node, unique_user_ids))

    id_to_node.update(
        dict(
            zip(
                non_dataset_users,
                range(len(id_to_node), len(id_to_node) + len(non_dataset_users)),
            )
        )
    )

    (
        pd.Series(id_to_node)
        .rename("node_id")
        .reset_index()
        .to_json(id_to_node_path, orient="records", lines=True)
    )

    edges["source"] = edges[source_column].map(id_to_node)
    edges["target"] = edges[target_column].map(id_to_node)

    # users that are not in the network: an empty row. we need it, but we don't need empty columns
    sparse_adjacency_matrix = dok_matrix(
        (len(elem_to_id), len(id_to_node)), dtype=np.int
    )

    for row in edges.itertuples():
        sparse_adjacency_matrix[
            getattr(row, "source"), getattr(row, "target")
        ] = getattr(row, "frequency")

    sparse_adjacency_matrix = sparse_adjacency_matrix.tocsr()
    save_npz(adjacency_matrix_path, sparse_adjacency_matrix)
    logging.info(
        f"{name} adjacency matrix ({repr(sparse_adjacency_matrix)}) -> {adjacency_matrix_path}"
    )


def group_user_interactions(
    interaction_dd,
    source_column,
    target_column,
    interaction_name,
    destination_path,
    overwrite=False,
):
    interactions_target = (
        destination_path / f"user.{interaction_name}_edges.all.json.gz"
    )

    if not overwrite and interactions_target.exists():
        logging.info(f"user.{interaction_name} exists! skipping.")
    else:
        interactions = (
            interaction_dd.groupby([source_column, target_column]).sum().compute()
        )

        interactions.reset_index().to_json(
            interactions_target, orient="records", lines=True, compression="gzip"
        )
        logging.info(
            f"user.{interaction_name} (#{len(interactions)}) -> {interactions_target}"
        )


def group_user_urls(
    urls_dd, elem_to_id, destination_path, min_freq=50, overwrite=False
):
    url_frequency_target = destination_path / "user.domains.all.json.gz"
    url_frequency_relevant_target = destination_path / "user.domains.relevant.json.gz"
    url_matrix_target = destination_path / "user.domains.matrix.npz"
    url_frequency_target = destination_path / "user.domains.all.json.gz"
    url_matrix_target = destination_path / "user.domains.matrix.npz"

    if not overwrite and url_matrix_target.exists():
        logging.info(f"user.domains matrix exists! skipping.")
        return

    urls = urls_dd.groupby(["user.id", "domain"]).sum().compute().reset_index()

    url_frequency = (
        urls.groupby("domain")["frequency"].sum().sort_values(ascending=False)
    )
    url_frequency.reset_index().to_json(
        url_frequency_target, compression="gzip", orient="records", lines=True
    )

    url_frequency_relevant = (
        url_frequency[url_frequency >= min_freq]
        .to_frame()
        .assign(token_id=lambda x: range(len(x)))
    )
    url_frequency_relevant.reset_index().to_json(
        url_frequency_relevant_target, compression="gzip", orient="records", lines=True
    )
    logging.info(
        f"user.domains relevant vocabulary ({len(url_frequency_relevant)}) -> {url_frequency_relevant_target}"
    )

    urls_relevant = urls[urls["domain"].isin(url_frequency_relevant.index)].set_index(
        ["user.id", "domain"]
    )

    url_to_id = url_frequency_relevant["token_id"].to_dict()

    dtm = dok_matrix(
        (max(elem_to_id.values()) + 1, max(url_to_id.values()) + 1), dtype=np.int
    )

    for row in urls_relevant.itertuples():
        if not row.Index[0] in elem_to_id:
            continue
        row_id = elem_to_id[row.Index[0]]
        column_id = url_to_id[row.Index[1]]
        dtm[row_id, column_id] = getattr(row, "frequency")

    dtm = dtm.tocsr()
    save_npz(url_matrix_target, dtm)
    logging.info(f"user.domains matrix ({repr(dtm)}) -> {url_matrix_target}")


def group_profile_domains(
    users_dd,
    elem_to_id,
    destination_path,
    min_freq=10,
    min_freq_tld=50,
    overwrite=False,
):
    profile_domains_target = destination_path / "user.profile_domains.relevant.json.gz"
    user_main_domain_matrix_target = (
        destination_path / "user.profile_domains.matrix.npz"
    )
    profile_tlds_target = destination_path / "user.profile_tlds.relevant.json.gz"
    user_tld_matrix_target = destination_path / "user.profile_tlds.matrix.npz"

    if not overwrite and (
        user_main_domain_matrix_target.exists() and user_tld_matrix_target.exists()
    ):
        logging.info(f"user.profile_domains exist! skipping.")
        return

    profile_urls = (
        users_dd[["user.id", "user.url"]]
        .compute()
        .dropna()
        .pipe(lambda x: x[x["user.url"].str.len() > 0].copy())
    )

    profile_urls["user.profile_domain"] = profile_urls["user.url"].map(get_domain)
    profile_urls["user.main_domain"] = (
        profile_urls["user.profile_domain"].str.split(".").str.slice(-2).str.join(".")
    )
    profile_urls["user.tld"] = (
        profile_urls["user.main_domain"].str.split(".").str.slice(-1).str.join("")
    )

    profile_domains = (
        profile_urls.groupby("user.main_domain").size().rename("frequency")
    )
    profile_domains = (
        profile_domains[profile_domains >= min_freq]
        .sort_values(ascending=False)
        .to_frame()
        .assign(token_id=lambda x: range(len(x)))
    )

    profile_domains.reset_index().to_json(
        profile_domains_target, compression="gzip", orient="records", lines=True
    )
    logging.info(f"user.profile_domains -> {profile_domains_target}")

    domain_to_id = dict(zip(profile_domains.index, range(len(profile_domains))))

    user_main_domain_matrix = dok_matrix(
        (max(elem_to_id.values()) + 1, max(domain_to_id.values()) + 1), dtype=np.int
    )

    for row in profile_urls.set_index(["user.id", "user.main_domain"]).itertuples():
        try:
            row_id = elem_to_id[row.Index[0]]
            column_id = domain_to_id[row.Index[1]]
        except KeyError:
            continue
        user_main_domain_matrix[row_id, column_id] = 1

    user_main_domain_matrix = user_main_domain_matrix.tocsr()

    save_npz(user_main_domain_matrix_target, user_main_domain_matrix)
    logging.info(
        f"user.main_domain matrix ({repr(user_main_domain_matrix)}) -> {user_main_domain_matrix_target}"
    )

    profile_tlds = profile_urls.groupby("user.tld").size().rename("frequency")
    profile_tlds = (
        profile_tlds[profile_tlds >= min_freq_tld]
        .sort_values(ascending=False)
        .to_frame()
        .assign(token_id=lambda x: range(len(x)))
    )

    profile_tlds.reset_index().to_json(
        profile_tlds_target, compression="gzip", orient="records", lines=True
    )
    logging.info(f"user.profile_tlds -> {profile_tlds_target}")

    tld_to_id = dict(zip(profile_tlds.index, range(len(profile_tlds))))

    user_tld_matrix = dok_matrix(
        (max(elem_to_id.values()) + 1, max(tld_to_id.values()) + 1), dtype=np.int64
    )

    for row in profile_urls.set_index(["user.id", "user.tld"]).itertuples():
        try:
            row_id = elem_to_id[row.Index[0]]
            column_id = tld_to_id[row.Index[1]]
        except KeyError:
            continue
        user_tld_matrix[row_id, column_id] = 1

    user_tld_matrix = user_tld_matrix.tocsr()

    save_npz(user_tld_matrix_target, user_tld_matrix)
    logging.info(
        f"user.tld_domain matrix ({repr(user_tld_matrix)}) -> {user_tld_matrix_target}"
    )


if __name__ == "__main__":
    log_fmt = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    logging.basicConfig(level=logging.INFO, format=log_fmt)

    # not used in this stub but often useful for finding various files
    project_dir = Path(__file__).resolve().parents[2]

    # find .env automagically by walking up directories until it's found, then
    # load up the .env entries as environment variables
    load_dotenv(find_dotenv())

    main()