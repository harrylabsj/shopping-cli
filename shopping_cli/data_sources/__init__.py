"""shopping-cli 数据源接入面（data hub v0.2.1 §3/§5）。

Kiwi merchant 只与 shopping-cli 沟通（不直连 ERP 或其他数据库）——
ERP / 商家本地库的接入在本包实现，作为 shopping-cli 内部的
CommerceDataSource 能力：

* ``erp_source.sync_erp_products`` —— ERP 分页拉取 → 校验 → upsert 本地
  ``products`` 表（本地成为缓存）+ ``source='erp'`` 标注；
* 本地录入（source='local'）为 LOCAL_AUTHORITATIVE；ERP 缓存为
  UPSTREAM_PROXY；同 SKU 冲突时 ERP 同步跳过本地手改行并报错
  （绝不静默合并冲突权威源）。
"""
