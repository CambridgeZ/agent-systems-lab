# 多文件结算示例仓库（Multi-File Checkout Repo）

本仓库模拟跨模块契约不一致导致的多文件缺陷：

- models.py：定义商品行 LineItem、优惠券 Coupon（含百分比与 max_discount_cents 封顶）以及结算结果 Receipt。
- discounts.py：compute_discount_cents(subtotal_cents, coupon) 负责计算应减免的分值，但漏掉了 coupon.max_discount_cents 上限封顶。
- checkout.py：build_receipt(items, coupon) 负责汇总小计并生成账单，但把 compute_discount_cents 返回的“减免分值”误当成了“折后应付总额”。
- tests/test_checkout.py：受保护的 5 个验收测试，覆盖无券、普通折扣、封顶上限、零元边界与非法参数。
