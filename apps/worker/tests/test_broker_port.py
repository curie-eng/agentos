"""The consumer-side broker port: redis.asyncio.Redis satisfies StreamBroker.

Structural-conformance only -- no Valkey behavior is mocked (the real consume /
ack / reclaim / delivery-cap behavior is covered against real Valkey in the
kernel + consumer suites). This pins that the one backing today is a drop-in
for the full port: xgroup_create, xreadgroup, xack, xautoclaim (group
semantics + crash recovery), xpending_range, xrange, xadd (#505 delivery
cap / dead-letter), plus xinfo_consumers and xclaim (#1532 dead-consumer
prompt reclaim).
"""

from curie_worker.broker import StreamBroker
from curie_worker.stream_consumer import StreamConsumer
from redis.asyncio import Redis


def test_async_redis_satisfies_broker_port() -> None:
    # Construction does not connect; no network call is made here.
    client = Redis(host="localhost", port=6379)
    assert isinstance(client, StreamBroker)


def test_stream_consumer_accepts_any_broker() -> None:
    class FakeBroker:
        async def xgroup_create(self, name, groupname, id="$", mkstream=False):  # noqa: ANN001, ANN201, A002
            return True

        async def xreadgroup(self, groupname, consumername, streams, count=None, block=None):  # noqa: ANN001, ANN201
            return []

        async def xack(self, name, groupname, *ids):  # noqa: ANN001, ANN201
            return 0

        async def xautoclaim(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
            return ("0-0", [])

        async def xpending_range(  # noqa: ANN201
            self, name, groupname, min, max, count, consumername=None, idle=None  # noqa: ANN001, A002
        ):
            return []

        async def xrange(self, name, min="-", max="+", count=None):  # noqa: ANN001, ANN201, A002
            return []

        async def xadd(self, name, fields):  # noqa: ANN001, ANN201
            return "0-1"

        async def xinfo_consumers(self, name, groupname):  # noqa: ANN001, ANN201
            return []

        async def xclaim(  # noqa: ANN201
            self,
            name,  # noqa: ANN001
            groupname,  # noqa: ANN001
            consumername,  # noqa: ANN001
            min_idle_time,  # noqa: ANN001
            message_ids,  # noqa: ANN001
            idle=None,  # noqa: ANN001
            time=None,  # noqa: ANN001
            retrycount=None,  # noqa: ANN001
            force=False,
            justid=False,
        ):
            return []

    broker = FakeBroker()
    assert isinstance(broker, StreamBroker)
    # A second broker drops into the consumer transport with no other change.
    consumer = StreamConsumer(broker)  # type: ignore[arg-type]
    assert consumer._redis is broker
