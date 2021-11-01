import numpy as np
import os
import json
import typing as tp
import time
import math
import requests

from concurrent.futures import ThreadPoolExecutor

from brownie import network, Contract
from datetime import datetime

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS, PointSettings

BLOCK_SUBGRAPH_ENDPOINT = "https://api.thegraph.com/subgraphs/name/decentraland/blocks-ethereum-mainnet"  # noqa


def get_b_q(timestamp: int) -> str:
    return """query {
                blocks(
                    first: 1,
                    orderBy: timestamp,
                    orderDirection: desc,
                    where: { timestamp_lt: %s }
                ) {
                    timestamp
                    number
                }
            } """ % (str(timestamp))


def get_config() -> tp.Dict:
    return {
        "token": os.getenv('INFLUXDB_TOKEN'),
        "org": os.getenv('INFLUXDB_ORG'),
        "bucket": os.getenv('INFLUXDB_BUCKET', 'ovl_univ3_1m'),
        "url": os.getenv("INFLUXDB_URL"),
        "window": os.getenv("WINDOW", 60)
    }


def create_client(config: tp.Dict) -> InfluxDBClient:
    return InfluxDBClient(
        url=config['url'],
        token=config['token'],
        debug=False
    )


def get_point_settings() -> PointSettings:
    point_settings = PointSettings(**{"type": "metrics-hourly"})
    point_settings.add_default_tag("influx-univ3", "ingest-data-frame")
    return point_settings


def POOL(addr: str, abi) -> Contract:
    # UniswapV3Pool contract
    return Contract.from_abi("unipool", addr, abi)


def get_quote_path() -> str:
    base = os.path.dirname(os.path.abspath(__file__))
    qp = 'constants/univ3_quotes.json'
    return os.path.join(base, qp)


def get_quotes() -> tp.List:
    quotes = []
    p = get_quote_path()
    with open(p) as f:
        data = json.load(f)
        quotes = data.get('quotes', [])
    return quotes


def get_uni_abi_path() -> str:
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, 'constants/univ3.abi.json')


def get_uni_abi() -> tp.Dict:
    with open(get_uni_abi_path()) as f:
        data = json.load(f)
    return data


def get_b_t(timestamp: int) -> tp.Tuple:

    q = {'query': get_b_q(timestamp)}

    retries = 1
    success = False

    while not success and retries < 5:
        try:
            result = json.loads(
                requests.post(BLOCK_SUBGRAPH_ENDPOINT, json=q).text
            )['data']['blocks'][0]
            success = True
        except Exception as e:
            wait = retries * 10
            err_cls = e.__class__
            err_msg = str(e)
            msg = f'''
            Error type = {err_cls}
            Error message = {err_msg}
            Wait {wait} secs
            '''
            print(msg)
            time.sleep(wait)
            retries += 1

    return (int(result['number']), int(result['timestamp']))


def get_calls(
        pair: Contract,
        t_from: int,
        t_to: int,
        t_period: int) -> tp.List:

    calls = []

    while t_from < t_to:
        calls.append((pair, t_from, t_period))
        t_from += t_period

    return calls


def find_start(api, quote, config) -> int:

    retries = 1
    success = False

    while not success and retries < 5:
        try:
            r = api.query_data_frame(org=config['org'], query=f'''
                from(bucket:"{config['bucket']}")
                    |> range(start:0, stop: now())
                    |> filter(fn: (r) => r["id"] == "{quote['id']}")
                    |> last()
            ''')
            success = True
        except Exception as e:
            wait = retries * 10
            err_cls = e.__class__
            err_msg = str(e)
            msg = f'''
            Error type = {err_cls}
            Error message = {err_msg}
            Wait {wait} secs
            '''
            print(msg)
            time.sleep(wait)
            retries += 1

    if (len(r.index) > 0):
        return math.floor(datetime.timestamp(
            r.iloc[0]['_time']))
    else:
        return quote['time_deployed'] + 1200


def read_cumulatives(args: tp.Tuple) -> tp.Tuple:

    (pair, t_at) = args
    (b, b_t) = get_b_t(t_at)
    (cum_tick, cum_liq) = pair.observe([0], block_identifier=b)
    return (b_t, cum_tick[0], cum_liq[0])


def index_pair(args: tp.Tuple):
    (write_api, quote, calls, config) = args

    print("INDEX PAIR")
    
    with ThreadPoolExecutor() as executor:
        for item in executor.map(read_cumulatives, calls):
            print("item", item)
            print('token0_name', quote['token0_name'])
            print('token1_name', quote['token1_name'])
            print("time:", item[0])
            print('tick_cumulative', float(item[1]))
            print('liquidity_cumulative', float(item[2]))

            point = Point("mem")\
                .tag("id", quote['id'])\
                .tag("token0_name", quote['token0_name'])\
                .tag("token1_name", quote['token1_name'])\
                .time(
                    datetime.utcfromtimestamp(float(item[0])),
                    WritePrecision.NS
                )

            point = point.field('tick_cumulative', float(item[1]))
            point = point.field('liquidity_cumulative', float(item[2]))

            retries = 1
            success = False
            while not success and retries < 5:
                try:
                    write_api.write(config['bucket'], config['org'], point)
                    print("written", quote['id'], datetime.fromtimestamp(
                        item[0]).strftime("%m/%d/%Y, %H:%M:%S"))
                    success = True
                except Exception as e:
                    wait = retries * 10
                    err_cls = e.__class__
                    err_msg = str(e)
                    msg = f'''
                    Error type = {err_cls}
                    Error message = {err_msg}
                    Wait {wait} secs
                    '''
                    print(msg)
                    time.sleep(wait)
                    retries += 1


def main():
    print(f"You are using the '{network.show_active()}' network")

    config = get_config()
    quotes = get_quotes()
    client = create_client(config)
    query_api = client.query_api()
    write_api = client.write_api(
        write_options=SYNCHRONOUS,
        point_settings=get_point_settings()
    )

    t_end = math.floor(time.time())
    while t_end <= time.time():
        get_uni_cumulatives(quotes, query_api, write_api, config, t_end)
        t_end = math.floor(time.time())


def get_uni_cumulatives(quotes, query_api, write_api, config, t_end):
    abi = get_uni_abi()

    for q in quotes:
        index_pair_calls = []
        q['fields'] = ['tick_cumulative']
        pool = POOL(q['pair'], abi)
        t_start = find_start(query_api, q, config)
        curr_time = datetime.fromtimestamp(t_end)\
            .strftime("%m/%d/%Y, %H:%M:%S")

        read_cumulatives_calls = [
            (pool, x) for x in np.arange(t_start, t_end, config['window'])
            ]

        index_pair_calls.append((write_api, q, read_cumulatives_calls, config))

        for i in map(index_pair, index_pair_calls):
            print(
                f'''
                Done executing for quote: {q};
                for timestamp until: {curr_time}
                '''
                )
