#
# Little stress test for the checkpointing and remote storage code.
#
# The test creates several tenants, and runs a simple workload on
# each tenant, in parallel. The test uses remote storage, and a tiny
# checkpoint_distance setting so that a lot of layer files are created.
#

import asyncio
from contextlib import closing
from typing import List, Tuple
from uuid import UUID

import pytest

from fixtures.neon_fixtures import NeonEnvBuilder, NeonEnv, Postgres, RemoteStorageKind, available_remote_storages, wait_for_last_record_lsn, wait_for_upload
from fixtures.utils import lsn_from_hex


async def tenant_workload(env: NeonEnv, pg: Postgres):
    pageserver_conn = await env.pageserver.connect_async()

    pg_conn = await pg.connect_async()

    tenant_id = await pg_conn.fetchval("show neon.tenant_id")
    timeline_id = await pg_conn.fetchval("show neon.timeline_id")

    await pg_conn.execute("CREATE TABLE t(key int primary key, value text)")
    for i in range(1, 100):
        await pg_conn.execute(
            f"INSERT INTO t SELECT {i}*1000 + g, 'payload' from generate_series(1,1000) g")

        # we rely upon autocommit after each statement
        # as waiting for acceptors happens there
        res = await pg_conn.fetchval("SELECT count(*) FROM t")
        assert res == i * 1000


async def all_tenants_workload(env: NeonEnv, tenants_pgs):
    workers = []
    for _, pg in tenants_pgs:
        worker = tenant_workload(env, pg)
        workers.append(asyncio.create_task(worker))

    # await all workers
    await asyncio.gather(*workers)


@pytest.mark.parametrize('remote_storatge_kind', available_remote_storages())
def test_tenants_many(neon_env_builder: NeonEnvBuilder, remote_storatge_kind: RemoteStorageKind):
    neon_env_builder.enable_remote_storage(
        remote_storage_kind=remote_storatge_kind,
        test_name='test_tenants_many',
    )

    env = neon_env_builder.init_start()

    tenants_pgs: List[Tuple[UUID, Postgres]] = []

    for _ in range(1, 5):
        # Use a tiny checkpoint distance, to create a lot of layers quickly
        tenant, _ = env.neon_cli.create_tenant(
            conf={
                'checkpoint_distance': '5000000',
                })
        env.neon_cli.create_timeline(f'test_tenants_many', tenant_id=tenant)

        pg = env.postgres.create_start(
            f'test_tenants_many',
            tenant_id=tenant,
        )
        tenants_pgs.append((tenant, pg))

    asyncio.run(all_tenants_workload(env, tenants_pgs))

    # Wait for the remote storage uploads to finish
    pageserver_http = env.pageserver.http_client()
    for tenant, pg in tenants_pgs:
        res = pg.safe_psql_many(
            ["SHOW neon.tenant_id", "SHOW neon.timeline_id", "SELECT pg_current_wal_flush_lsn()"])
        tenant_id = res[0][0][0]
        timeline_id = res[1][0][0]
        current_lsn = lsn_from_hex(res[2][0][0])

        # wait until pageserver receives all the data
        wait_for_last_record_lsn(pageserver_http, UUID(tenant_id), UUID(timeline_id), current_lsn)

        # run final checkpoint manually to flush all the data to remote storage
        env.pageserver.safe_psql(f"checkpoint {tenant_id} {timeline_id}")
        wait_for_upload(pageserver_http, UUID(tenant_id), UUID(timeline_id), current_lsn)
