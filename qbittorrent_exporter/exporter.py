import faulthandler
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from enum import StrEnum, auto
from typing import Any, Iterable

from prometheus_client import start_http_server
from prometheus_client.core import REGISTRY, CounterMetricFamily, GaugeMetricFamily
from pythonjsonlogger import jsonlogger
from qbittorrentapi import Client, TorrentStates

# Enable dumps on stderr in case of segfault
faulthandler.enable()
logger = logging.getLogger()


class MetricType(StrEnum):
    """
    Represents possible metric types (used in this project).
    """
    GAUGE = auto()
    COUNTER = auto()


@dataclass
class Metric:
    """
    Contains data and metadata about a single counter or gauge.
    """
    name: str
    value: Any
    labels: dict[str, str] = field(default_factory=lambda: {})
    help_text: str = ""
    metric_type: MetricType = MetricType.GAUGE


class QbittorrentMetricsCollector:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.server = f"{config['host']}:{config['port']}"
        self.protocol = "http"

        if config["url_base"]:
            self.server = f"{self.server}/{config['url_base']}"
        if config["ssl"] or config["port"] == "443":
            self.protocol = "https"
        self.connection_string = f"{self.protocol}://{self.server}"
        self.client = Client(
            host=self.connection_string,
            username=config["username"],
            password=config["password"],
            VERIFY_WEBUI_CERTIFICATE=config["verify_webui_certificate"],
        )

    def collect(self) -> Iterable[GaugeMetricFamily | CounterMetricFamily]:
        """
        Yields Prometheus metrics etter at vi har gruppert alle samples med samme
        (name, metric_type) i én enkelt MetricFamily.

        Viktig: Dersom flere Metric-objekter med samme navn men ulik help_text
        oppstår, vil vi logge en warning og benytte help_text fra den første vi møtte.
        Dette er nødvendig ettersom Prometheus krever at en metrikk kun har én HELP-linje.
        """
        metrics: list[Metric] = self.get_qbittorrent_metrics()
        grouped = {}

        # Gruppér metrics etter (name, metric_type)
        for metric in metrics:
            key = (metric.name, metric.metric_type)
            if key not in grouped:
                grouped[key] = {
                    "label_keys": list(metric.labels.keys()),
                    "samples": [],
                    "help_text": metric.help_text,
                }
            else:
                # Dersom help_text for samme metric-navn er ulik, logg en warning.
                if grouped[key]["help_text"] != metric.help_text:
                    logger.warning(
                        f"Conflicting help texts for metric {metric.name}: "
                        f"'{grouped[key]['help_text']}' vs '{metric.help_text}'. Using the first one."
                    )
            grouped[key]["samples"].append((list(metric.labels.values()), metric.value))

        for (name, metric_type), data in grouped.items():
            if metric_type == MetricType.COUNTER:
                prom_metric = CounterMetricFamily(name, data["help_text"], labels=data["label_keys"])
            else:
                prom_metric = GaugeMetricFamily(name, data["help_text"], labels=data["label_keys"])
            for label_values, value in data["samples"]:
                prom_metric.add_metric(label_values, value)
            yield prom_metric

    def get_qbittorrent_metrics(self) -> list[Metric]:
        """
        Calls and combines qbittorrent state metrics with torrent metrics.
        """
        metrics: list[Metric] = []
        metrics.extend(self._get_qbittorrent_status_metrics())
        metrics.extend(self._get_qbittorrent_torrent_tags_metrics())
        metrics.extend(self._get_qbittorrent_by_torrent_metrics())
        return metrics

    def _get_qbittorrent_by_torrent_metrics(self) -> list[Metric]:
        if not self.config.get("export_metrics_by_torrent", False):
            return []
        torrents = self._fetch_torrents()
        metrics: list[Metric] = []
        for torrent in torrents:
            metrics.append(
                Metric(
                    name=f"{self.config['metrics_prefix']}_torrent_size",
                    value=torrent["size"],
                    labels={
                        "name": torrent["name"],
                        "category": torrent["category"],
                        "server": self.server,
                    },
                    help_text="Size of the torrent",
                )
            )
            metrics.append(
                Metric(
                    name=f"{self.config['metrics_prefix']}_torrent_downloaded",
                    value=torrent["downloaded"],
                    labels={
                        "name": torrent["name"],
                        "category": torrent["category"],
                        "server": self.server,
                    },
                    help_text="Downloaded data for the torrent",
                )
            )
        return metrics

    def _get_qbittorrent_status_metrics(self) -> list[Metric]:
        """
        Returns metrics about the state of the qbittorrent server.
        """
        maindata: dict[str, Any] = {}
        version: str = ""
        # Fetch data from API
        try:
            maindata = self.client.sync_maindata()
            version = self.client.app.version
        except Exception as e:
            logger.error(f"Couldn't get server info: {e}")

        server_state = maindata.get("server_state", {})

        return [
            Metric(
                name=f"{self.config['metrics_prefix']}_up",
                value=bool(server_state),
                labels={"version": version, "server": self.server},
                help_text=(
                    "Whether the qBittorrent server is answering requests from this exporter. "
                    "A `version` label with the server version is added."
                ),
            ),
            Metric(
                name=f"{self.config['metrics_prefix']}_connected",
                value=server_state.get("connection_status", "") == "connected",
                labels={"server": self.server},
                help_text="Whether the qBittorrent server is connected to the Bittorrent network.",
            ),
            Metric(
                name=f"{self.config['metrics_prefix']}_firewalled",
                value=server_state.get("connection_status", "") == "firewalled",
                labels={"server": self.server},
                help_text="Whether the qBittorrent server is connected to the Bittorrent network but is behind a firewall.",
            ),
            Metric(
                name=f"{self.config['metrics_prefix']}_dht_nodes",
                value=server_state.get("dht_nodes", 0),
                labels={"server": self.server},
                help_text="Number of DHT nodes connected to.",
            ),
            Metric(
                name=f"{self.config['metrics_prefix']}_dl_info_data",
                value=server_state.get("dl_info_data", 0),
                labels={"server": self.server},
                help_text="Data downloaded since the server started, in bytes.",
                metric_type=MetricType.COUNTER,
            ),
            Metric(
                name=f"{self.config['metrics_prefix']}_up_info_data",
                value=server_state.get("up_info_data", 0),
                labels={"server": self.server},
                help_text="Data uploaded since the server started, in bytes.",
                metric_type=MetricType.COUNTER,
            ),
            Metric(
                name=f"{self.config['metrics_prefix']}_alltime_dl",
                value=server_state.get("alltime_dl", 0),
                labels={"server": self.server},
                help_text="Total historical data downloaded, in bytes.",
                metric_type=MetricType.COUNTER,
            ),
            Metric(
                name=f"{self.config['metrics_prefix']}_alltime_ul",
                value=server_state.get("alltime_ul", 0),
                labels={"server": self.server},
                help_text="Total historical data uploaded, in bytes.",
                metric_type=MetricType.COUNTER,
            ),
        ]

    def _fetch_categories(self) -> dict:
        """Fetches all categories in use from qbittorrent."""
        try:
            categories = dict(self.client.torrent_categories.categories)
            for key, value in categories.items():
                categories[key] = dict(value)  # type: ignore
            return categories
        except Exception as e:
            logger.error(f"Couldn't fetch categories: {e}")
            return {}

    def _fetch_torrents(self) -> list[dict]:
        """Fetches torrents from qbittorrent"""
        try:
            return [dict(_attr_dict) for _attr_dict in self.client.torrents.info()]
        except Exception as e:
            logger.error(f"Couldn't fetch torrents: {e}")
            return []

    def _filter_torrents_by_category(self, category: str, torrents: list[dict]) -> list[dict]:
        """Filters torrents by the given category."""
        return [
            torrent
            for torrent in torrents
            if torrent["category"] == category or (category == "Uncategorized" and torrent["category"] == "")
        ]

    def _filter_torrents_by_state(self, state: TorrentStates, torrents: list[dict]) -> list[dict]:
        """Filters torrents by the given state."""
        return [torrent for torrent in torrents if torrent["state"] == state.value]

    def _construct_metric(self, state: str, category: str, count: int) -> Metric:
        """Constructs and returns a metric object with a torrent count and appropriate labels."""
        # Merk at help_text her varierer med state og category,
        # men for groupingens skyld vil vi kombinere alle qbittorrent_torrents_count-metrics til ett.
        return Metric(
            name=f"{self.config['metrics_prefix']}_torrents_count",
            value=count,
            labels={
                "status": state,
                "category": category,
                "server": self.server,
            },
            help_text=f"Number of torrents filtered by status and category",
        )

    def _get_qbittorrent_torrent_tags_metrics(self) -> list[Metric]:
        categories = self._fetch_categories()
        torrents = self._fetch_torrents()

        metrics: list[Metric] = []
        # Legg til "Uncategorized" hvis den ikke finnes fra før
        categories["Uncategorized"] = {"name": "Uncategorized", "savePath": ""}

        for category in categories:
            category_torrents = self._filter_torrents_by_category(category, torrents)
            for state in TorrentStates:
                state_torrents = self._filter_torrents_by_state(state, category_torrents)
                # Her bruker vi _construct_metric, og vi setter en fast help_text for å sikre
                # at alle samples med samme metric-navn får samme HELP-linje.
                metric = self._construct_metric(state.value, category, len(state_torrents))
                metrics.append(metric)

        return metrics


class ShutdownSignalHandler:
    def __init__(self):
        self.shutdown_count: int = 0

        # Register signal handler
        signal.signal(signal.SIGINT, self._on_signal_received)
        signal.signal(signal.SIGTERM, self._on_signal_received)

    def is_shutting_down(self):
        return self.shutdown_count > 0

    def _on_signal_received(self, sig, frame):
        if self.shutdown_count > 1:
            logger.warning("Forcibly killing exporter")
            sys.exit(1)
        logger.info("Exporter is shutting down")
        self.shutdown_count += 1


def _get_config_value(key: str, default: str = "") -> str:
    input_path = os.environ.get("FILE__" + key, None)
    if input_path is not None:
        try:
            with open(input_path, "r") as input_file:
                return input_file.read().strip()
        except IOError as e:
            logger.error(f"Unable to read value for {key} from {input_path}: {str(e)}")
    return os.environ.get(key, default)


def get_config() -> dict:
    """Loads all config values."""
    return {
        "host": _get_config_value("QBITTORRENT_HOST", ""),
        "port": _get_config_value("QBITTORRENT_PORT", ""),
        "ssl": (_get_config_value("QBITTORRENT_SSL", "False") == "True"),
        "url_base": _get_config_value("QBITTORRENT_URL_BASE", ""),
        "username": _get_config_value("QBITTORRENT_USER", ""),
        "password": _get_config_value("QBITTORRENT_PASS", ""),
        "exporter_address": _get_config_value("EXPORTER_ADDRESS", "0.0.0.0"),
        "exporter_port": int(_get_config_value("EXPORTER_PORT", "8000")),
        "log_level": _get_config_value("EXPORTER_LOG_LEVEL", "INFO"),
        "metrics_prefix": _get_config_value("METRICS_PREFIX", "qbittorrent"),
        "export_metrics_by_torrent": (_get_config_value("EXPORT_METRICS_BY_TORRENT", "False") == "True"),
        "verify_webui_certificate": (_get_config_value("VERIFY_WEBUI_CERTIFICATE", "True") == "True"),
    }


def main():
    # Init logger slik at den kan brukes
    logHandler = logging.StreamHandler()
    formatter = jsonlogger.JsonFormatter(
        "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    logHandler.setFormatter(formatter)
    logger.addHandler(logHandler)
    logger.setLevel("INFO")  # default til konfigurasjon lastes

    config = get_config()
    # Sett loggnivået etter at konfigurasjonen er lastet
    logger.setLevel(config["log_level"])

    # Register signal handler
    signal_handler = ShutdownSignalHandler()

    if not config["host"]:
        logger.error("No host specified, please set QBITTORRENT_HOST environment variable")
        sys.exit(1)
    if not config["port"]:
        logger.error("No port specified, please set QBITTORRENT_PORT environment variable")
        sys.exit(1)

    # Register our custom collector
    logger.info("Exporter is starting up")
    REGISTRY.register(QbittorrentMetricsCollector(config))  # type: ignore

    # Start server
    start_http_server(config["exporter_port"], config["exporter_address"])
    logger.info(f"Exporter listening on {config['exporter_address']}:{config['exporter_port']}")

    while not signal_handler.is_shutting_down():
        time.sleep(1)

    logger.info("Exporter has shutdown")


if __name__ == "__main__":
    main()
