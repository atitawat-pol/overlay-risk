import pandas as pd
import numpy as np
import os
import json
import typing as tp
import logging

from datetime import datetime, timedelta
from scipy.stats import norm

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS, PointSettings

# Display all columns on print
pd.set_option('display.max_columns', None)

# Fixed point resolution of price cumulatives
PC_RESOLUTION = 112


def get_config() -> tp.Dict:
    '''
    Returns a `config` dict containing InfluxDB configuration parameters

    Outputs:
        [tp.Dict]
        token   [str]:  INFLUXDB_TOKEN env, InfluxDB token
        org     [str]:  INFLUXDB_ORG env, InfluxDB organization
        bucket  [str]:  INFLUXDB_BUCKET env, InfluxDB bucket
        source  [str]:  INFLUXDB_SOURCE env, InfluxDB source bucket
        url     [str]:  INFLUXDB_URL env, InfluxDB url
    '''
    return {
        "token": os.getenv('INFLUXDB_TOKEN'),
        "org": os.getenv('INFLUXDB_ORG'),
        "bucket": os.getenv('INFLUXDB_BUCKET', "ovl_metrics_univ3"),
        "source": os.getenv('INFLUXDB_SOURCE', "ovl_univ3"),
        "url": os.getenv("INFLUXDB_URL"),
    }


def create_client(config: tp.Dict) -> InfluxDBClient:
    '''
    Returns an InfluxDBClient initialized with config `url` and `token` params
    returned by `get_config`

    Inputs:
        [tp.Dict]
        token   [str]:  INFLUXDB_TOKEN env representing an InfluxDB token
        url     [str]:  INFLUXDB_URL env representing an InfluxDB url

    Outputs:
        [InfluxDBClient]: InfluxDB client connection instance
    '''
    return InfluxDBClient(
            url=config['url'],
            token=config['token'],
            debug=False)


def get_point_settings() -> PointSettings:
    point_settings = PointSettings(**{"type": "metrics-hourly"})
    point_settings.add_default_tag("influx-metrics", "ingest-data-frame")
    return point_settings


def get_params() -> tp.Dict:
    '''
    Returns a `params` dict for parameters to use in statistical estimates.
    Generates metrics for 1h TWAP over last 30 days with VaR stats for next 7
    days.

    Outputs:
        [tp.Dict]
        points  [int]:          1 mo of data behind to estimate MLEs
        window  [int]:          1h TWAPs (assuming `ovl_sushi` ingested every
                                10m)
        period  [int]:          10m periods [s]
        tolerance  [int]:       Tolerance within which `period` can
                                be inaccurate
        alpha   List[float]:    alpha uncertainty in VaR calc
        n:      List[int]:      number of periods into the future over which
                                VaR is calculated
    '''
    return {
        "points": 30,
        "window": 60,
        "period": 60,
        "tolerance": 10,
        "alpha": [0.05, 0.01, 0.001, 0.0001],
        "n": [144, 1008, 2016, 4320],
    }


def get_quote_path() -> str:
    '''
    Returns full path to `quotes.json` file.

    Outputs:
        [str]:  Full path to `quotes.json` file

    '''
    base = os.path.dirname(os.path.abspath(__file__))
    qp = 'constants/univ3_quotes.json'
    return os.path.join(base, qp)


def get_quotes() -> tp.List:
    '''
    Loads from `scripts/constants/quotes.json` and return a List
    of quote dicts for quote data fetched from SushiSwap.

    Output:
        [tp.List[dict]]
        id         [str]:   Name of swap pair
        pair       [str]:   Contract address of swap pair
        token0     [str]:   Contract address of token 0 in swap pair
        token1     [str]:   Contract address of token 1 in swap pair
        is_price0  [bool]:  If true, use the TWAP value calculated from the
                            `priceCumulative0` storage variable:
                            `price0 = num_token_1 / num_token_0`

                            If false, use the TWAP value calculated from the
                            `priceCumulative1` storage variable
        amount_in  [float]:  Swap input amount
    '''
    quotes = []
    p = get_quote_path()
    with open(p) as f:
        data = json.load(f)
        quotes = data.get('quotes', [])
    return quotes


def get_price_fields() -> (str, str):
    return 'tick_cumulative'


def get_price_cumulatives(
        query_api,
        cfg: tp.Dict,
        q: tp.Dict,
        p: tp.Dict) -> (int, tp.List[pd.DataFrame]):
    '''
    Fetches `historical time series of priceCumulative` values for the last
    `params['points']` number of days for id `quote['id']` from the config
    bucket `source` in `org`.

    Inputs:
        query_api  [QueryApi]:  InfluxDB client QueryApi instance
        cfg        [tp.Dict]:   Contains InfluxDB configuration parameters
          token   [str]:  INFLUXDB_TOKEN env, InfluxDB token
          org     [str]:  INFLUXDB_ORG env, InfluxDB organization
          bucket  [str]:  INFLUXDB_BUCKET env, InfluxDB bucket
          source  [str]:  INFLUXDB_SOURCE env, InfluxDB source bucket
          url     [str]:  INFLUXDB_URL env, InfluxDB url
        q          [tp.Dict]:   Quote pair entry fetched from SushiSwap
          id         [str]:   Name of swap pair
          pair       [str]:   Contract address of swap pair
          token0     [str]:   Contract address of token 0 in swap pair
          token1     [str]:   Contract address of token 1 in swap pair
          is_price0  [bool]:  If true, use the TWAP value calculated from the
                              `priceCumulative0` storage variable:
                              `price0 = num_token_1 / num_token_0`

                              If false, use the TWAP value calculated from the
                              `priceCumulative1` storage variable
          amount_in  [float]:  Swap input amount
        p          [tp.Dict]:  Parameters to use in statistical estimates
          points  [int]:          1 mo of data behind to estimate mles
          window  [int]:          1h TWAPs (assuming ovl_sushi ingested every
                                  10m) [s]
          period  [int]:          10m periods [s]
          alpha   List[float]:    alpha uncertainty in VaR calc
          n:      List[int]:      number of periods into the future over which
                                  VaR is calculated

    Outputs:
        [tuple]: Assembled from query
          timestamp          [int]:               Most recent timestamp of data
                                                  in `priceCumulative`
                                                  dataframes
          priceCumulatives0  [pandas.DataFrame]:
            _time  [int]:  Unix timestamp
            _field [str]:  Price field, `price0Cumulative`
            _value [int]:  `priceCumulative0` at unix timestamp `_time`
          priceCumulatives1  [pandas.DataFrame]:
            _time  [int]:  Unix timestamp
            _field [str]:  Price field, `price1Cumulative`
            _value [int]:  `priceCumulative1` at unix timestamp `_time`
    '''
    qid = q['id']
    points = p['points']
    bucket = cfg['source']
    org = cfg['org']

    print(f'Fetching prices for {qid} ...')
    query = f'''
        from(bucket:"{bucket}") |> range(start: -{points}d)
            |> filter(fn: (r) => r["_measurement"] == "mem")
            |> filter(fn: (r) => r["id"] == "{qid}")
    '''
    df = query_api.query_data_frame(query=query, org=org)
    if type(df) == list:
        df = pd.concat(df, ignore_index=True)

    # print('df: ', df)
    # Filter then separate the df into p0c and p1c dataframes
    df_filtered = df.filter(items=['_time', '_field', '_value'])
    pc_field = get_price_fields()

    df_p0c = df_filtered[df_filtered['_field'] == pc_field]
    df_p0c.loc[:, '_field'] = 'tick_cumulative0'
    df_p0c = df_p0c.sort_values(by='_time', ignore_index=True)

    df_p1c = df_p0c.copy()
    df_p1c.loc[:, '_field'] = 'tick_cumulative1'
    df_p1c.loc[:, '_value'] = df_p0c.loc[:, '_value']

    # Get the last timestamp
    timestamp = datetime.timestamp(df_p0c['_time'][len(df_p0c['_time'])-1])

    return timestamp, [df_p0c, df_p1c]


def dynamic_window(
        df: pd.DataFrame,
        max_rows: int,
        window: int
        ) -> pd.DataFrame:
    '''
    Computes the window size in terms of rows such that there is as much data
    as there are seconds specified in the `window` variable.
    '''

    for i in range(1, int(max_rows+1)):
        df['lag_time'] = df[['_time']].shift(i)
        df[i] =\
            (pd.to_datetime(df['_time']) - pd.to_datetime(df['lag_time']))\
            .dt.total_seconds()
        df[i] = abs(df[i] - window)
        df.drop(['lag_time'], axis=1, inplace=True)

    min_df = df[[i for i in range(1, int(max_rows+1))]]\
        .idxmin(axis="columns")

    df.dropna(inplace=True)
    df = df.join(pd.DataFrame(min_df, columns=['dynamic_window']))
    df['dynamic_window'] = df['dynamic_window'].astype(int)
    return df


def delta_window(
        row: pd.Series,
        values: pd.Series,
        lookback: pd.Series
        ) -> pd.Series:
    '''
    Computes difference based on window sizes specified in `lookback`
    '''

    loc = values.index.get_loc(row.name)
    lb = lookback.loc[row.name]
    return values.iloc[loc] - values.iloc[loc-lb]


def get_twap(pc: pd.DataFrame, q: tp.Dict, p: tp.Dict) -> pd.DataFrame:
    window = p['window']
    period = p['period']
    tolerance = p['tolerance']
    upper_limit = window + tolerance
    lower_limit = window - tolerance

    max_rows = ((window/period)+1) * 2

    pc = dynamic_window(pc, int(max_rows), int(window))
    pc['dp'] = pc.apply(
            delta_window,
            values=pc['_value'],
            lookback=pc['dynamic_window'],
            axis=1)
    pc['dt'] = pc.apply(
            delta_window,
            values=pc['_time'],
            lookback=pc['dynamic_window'],
            axis=1).dt.total_seconds()

    pc = pc[(pc['dt'] > 0)]
    pc = pc[((pc['dt'] <= upper_limit) & (pc['dt'] >= lower_limit))]
    pc.reset_index(inplace=True)
    # with NaNs filtered out
    log_p = pc['dp'] / pc['dt']
    twap_112 = (log_p.apply(lambda x: np.power(1.0001, x))).to_numpy()
    twaps = twap_112[np.logical_not(np.isnan(twap_112))]
    if pc.loc[0, '_field'] == 'tick_cumulative1':
        twaps = 1/twaps

    # window times
    window_times = pc['dt'].to_numpy()
    window_times = window_times[np.logical_not(np.isnan(window_times))]

    # window close timestamps
    t = pc.filter(items=['_time'])\
        .applymap(datetime.timestamp)\
        .rolling(window=1)\
        .apply(lambda w: w[-1], raw=True)
    ts = t['_time'].to_numpy()
    ts = ts[np.logical_not(np.isnan(ts))]

    df = pd.DataFrame(data=[ts, window_times, twaps]).T
    df.columns = ['timestamp', 'window', 'twap']

    # filter out any twaps that are less than or equal to 0;
    # TODO: why? injestion from sushi?
    df = df[df['twap'] > 0]
    return df


def get_twaps(
        pcs: tp.List[pd.DataFrame],
        q: tp.Dict,
        p: tp.Dict) -> tp.List[pd.DataFrame]:
    return [get_twap(pc, q, p) for pc in pcs]


def get_samples_from_twaps(
        twaps: tp.List[pd.DataFrame]) -> tp.List[np.ndarray]:
    return [twap['twap'].to_numpy() for twap in twaps]


# Calcs VaR * d^n normalized for initial imbalance
# See: https://oips.overlay.market/notes/note-4
def calc_vars(mu: float,
              sig_sqrd: float,
              t: int,
              n: int,
              alphas: np.ndarray) -> np.ndarray:
    '''
    Calculates bracketed term:
        [e**(mu * n * t + sqrt(sig_sqrd * n * t) * Psi^{-1}(1 - alpha))]
    in Value at Risk (VaR) expressions for each alpha value in the `alphas`
    numpy array. SEE: https://oips.overlay.market/notes/note-4

    Inputs:
      mu        [float]:
      sig_sqrd  [float]:
      t         [int]:
      n         [int]:
      alphas    [np.ndarray]:

    Outputs:
      [np.ndarray]:  Array of calculated values for each `alpha`

    '''
    sig = np.sqrt(sig_sqrd)
    q = 1 - alphas
    pow = mu * n * t + sig * np.sqrt(n * t) * norm.ppf(q)
    nn = np.exp(pow) - 1
    return nn


def get_stat(
        timestamp: int,
        sample: np.ndarray,
        q: tp.Dict,
        p: tp.Dict) -> pd.DataFrame:
    t = p["period"]

    # mles
    rs = [
        np.log(sample[i]/sample[i-1]) for i in range(1, len(sample), 1)
    ]
    mu = float(np.mean(rs) / t)
    ss = float(np.var(rs) / t)

    # VaRs for 5%, 1%, 0.1%, 0.01% alphas, n periods into the future
    alphas = np.array(p["alpha"])
    ns = np.array(p["n"])
    vars = [calc_vars(mu, ss, t, n, alphas) for n in ns]
    var_labels = [
        f'VaR alpha={alpha} n={n}'
        for n in ns
        for alpha in alphas
    ]

    data = np.concatenate(([timestamp, mu, ss], *vars), axis=None)

    df = pd.DataFrame(data=data).T
    df.columns = ['timestamp', 'mu', 'sigSqrd', *var_labels]
    return df


def get_stats(
        timestamp: int,
        samples: tp.List[np.ndarray],
        q: tp.Dict,
        p: tp.Dict) -> tp.List[pd.DataFrame]:
    return [get_stat(timestamp, sample, q, p) for sample in samples]


# SEE: get_params() for more info on setup
def main():
    print("You are using data from the mainnet network")
    config = get_config()
    params = get_params()
    quotes = get_quotes()
    client = create_client(config)
    query_api = client.query_api()
    write_api = client.write_api(
        write_options=SYNCHRONOUS,
        point_settings=get_point_settings(),
    )

    for q in quotes:
        print('id', q['id'])
        try:
            timestamp, pcs = get_price_cumulatives(query_api, config, q,
                                                   params)
            # Calculate difference between max and min date.
            data_days = pcs[0]['_time'].max() - pcs[0]['_time'].min()
            print(
                f"Number of days between latest and first "
                f"data point: {data_days}"
            )

            if data_days < timedelta(days=params['points']-1):
                print(
                    f"This pair has less than {params['points']-1} days of "
                    f"data, therefore it is not being ingested "
                    f"to {config['bucket']}"
                )
                # continue

            twaps = get_twaps(pcs, q, params)
            print('timestamp', timestamp)
            print('twaps', twaps)

            # Calc stats for each twap (NOT inverse of each other)
            samples = get_samples_from_twaps(twaps)
            stats = get_stats(timestamp, samples, q, params)
            print('stats', stats)

            for i, stat in enumerate(stats):
                token_name = q[f'token{i}_name']
                point = Point("mem")\
                    .tag("id", q['id'])\
                    .tag('token_name', token_name)\
                    .tag("_type", f"price{i}Cumulative")\
                    .time(
                        datetime.utcfromtimestamp(float(stat['timestamp'])),
                        WritePrecision.NS
                    )

                for col in stat.columns:
                    if col != 'timestamp':
                        point = point.field(col, float(stat[col]))

                print(f"Writing {q['id']} for price{i}Cumulative to api ...")
                write_api.write(config['bucket'], config['org'], point)

        except Exception as e:
            print("Failed to write quote stats to influx")
            logging.exception(e)

    client.close()


if __name__ == '__main__':
    main()