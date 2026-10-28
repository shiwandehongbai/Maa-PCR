"""Fixed CN server and client identities; unknown values never select Bilibili."""
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class GameServer:
    server_id: str
    name: str
    package: str
    channel: bool = True

    @property
    def root(self):
        return ('https://l1-prod-uo-gs-gzlj.bilibiligame.net/' if self.channel
                else 'https://l3-prod-all-gs-gzlj.bilibiligame.net/')

    @property
    def res_key(self):
        return 'd145b29050641dac2f8b19df0afe0e59' if self.channel else 'ab00a0a6dd915a052a2ef7fd649083e5'

    @property
    def platform_id(self):
        return '4' if self.channel else '2'

    @property
    def closes_client_before_login(self):
        return self.server_id in ('cn-4399', 'cn-mumu', 'cn-tencent')

    @property
    def transient_rotation(self):
        return self.server_id in ('cn-vivo', 'cn-4399', 'cn-aligames')


SERVERS = MappingProxyType({s.server_id: s for s in (
    GameServer('cn-bilibili', '国服 Bilibili', 'com.bilibili.priconne', False),
    GameServer('cn-xiaomi', '小米渠道服', 'com.bilibili.priconne.mi'),
    GameServer('cn-huawei', '华为渠道服', 'com.bilibili.priconne.huawei'),
    GameServer('cn-vivo', 'vivo 渠道服', 'com.bilibili.priconne.vivo'),
    GameServer('cn-oppo', 'OPPO 渠道服', 'com.bilibili.priconne.nearme.gamecenter'),
    GameServer('cn-aligames', '九游渠道服', 'com.bilibili.priconne.aligames'),
    GameServer('cn-4399', '4399 渠道服', 'com.bilibili.priconne.m4399'),
    GameServer('cn-mumu', 'MuMu 渠道服', 'com.bilibili.priconne.yofun.mumu'),
    GameServer('cn-tencent', '应用宝渠道服', 'com.tencent.tmgp.bilibili.priconne'),
)})


def get_server(server_id='cn-bilibili'):
    if type(server_id) is not str or server_id not in SERVERS:
        raise ValueError('unknown_game_server')
    return SERVERS[server_id]
