import pickle
from pathlib import Path

import pandas as pd

import pytest

import runhouse as rh


# https://docs.pytest.org/en/6.2.x/fixture.html#conftest-py-sharing-fixtures-across-multiple-files


@pytest.fixture
def blob_data():
    return pickle.dumps(list(range(50)))


@pytest.fixture
def local_folder():
    local_folder = rh.folder(path=Path.cwd() / "tests_tmp")
    yield local_folder
    local_folder.delete_in_system()
    assert not local_folder.exists_in_system()


# ----------------- Tables -----------------
@pytest.fixture
def huggingface_table():
    from datasets import load_dataset

    dataset = load_dataset("yelp_review_full", split="train[:1%]")
    return dataset


@pytest.fixture
def arrow_table():
    import pyarrow as pa

    df = pd.DataFrame(
        {
            "int": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
            "str": ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"],
        }
    )
    arrow_table = pa.Table.from_pandas(df)
    return arrow_table


@pytest.fixture
def cudf_table():
    import cudf

    gdf = cudf.DataFrame(
        {"id": [1, 2, 3, 4, 5, 6], "grade": ["a", "b", "b", "a", "a", "e"]}
    )
    return gdf


@pytest.fixture
def pandas_table():
    df = pd.DataFrame(
        {"id": [1, 2, 3, 4, 5, 6], "grade": ["a", "b", "b", "a", "a", "e"]}
    )
    return df


@pytest.fixture
def dask_table():
    import dask.dataframe as dd

    index = pd.date_range("2021-09-01", periods=2400, freq="1H")
    df = pd.DataFrame({"a": range(2400), "b": list("abcaddbe" * 300)}, index=index)
    ddf = dd.from_pandas(df, npartitions=10)
    return ddf


@pytest.fixture
def ray_table():
    import ray

    ds = ray.data.range(10000)
    return ds


# ----------------- Clusters -----------------


@pytest.fixture
def cluster(request):
    """Parametrize over multiple fixtures - useful for running the same test on multiple hardware types."""
    # Example: @pytest.mark.parametrize("cluster", ["v100_gpu_cluster", "k80_gpu_cluster"], indirect=True)"""
    return request.getfixturevalue(request.param)


@pytest.fixture
def cpu_cluster():
    return rh.cluster("^rh-cpu").up_if_not()


@pytest.fixture
def cpu_cluster_2():
    return rh.cluster(
        name="other-cpu", instance_type="CPU:2+", provider="aws"
    ).up_if_not()


@pytest.fixture
def v100_gpu_cluster():
    return rh.cluster("^rh-v100", provider="aws").up_if_not()


@pytest.fixture
def k80_gpu_cluster():
    return rh.cluster(name="rh-k80", instance_type="K80:1", provider="aws").up_if_not()


@pytest.fixture
def a10g_gpu_cluster():
    return rh.cluster(
        name="rh-a10x", instance_type="g5.2xlarge", provider="aws"
    ).up_if_not()
