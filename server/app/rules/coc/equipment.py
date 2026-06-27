"""COC 7th Edition 装备目录"""

from dataclasses import dataclass, field


@dataclass
class Equipment:
    name: str
    category: str
    era: list[str] = field(default_factory=lambda: ["1920s", "modern"])
    min_credit: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "category": self.category,
            "era": self.era,
            "min_credit": self.min_credit,
        }


COC_EQUIPMENT: list[Equipment] = [
    # ---- 个人物品 ----
    Equipment("火柴/打火机", "个人物品"),
    Equipment("笔记本与铅笔", "个人物品"),
    Equipment("怀表", "个人物品", era=["1920s"]),
    Equipment("手表", "个人物品", era=["modern"]),
    Equipment("钱包", "个人物品"),
    Equipment("香烟盒", "个人物品"),
    Equipment("小镜子", "个人物品"),
    Equipment("幸运符/护身符", "个人物品"),
    Equipment("手帕", "个人物品"),
    Equipment("雨伞", "个人物品"),
    Equipment("眼镜", "个人物品"),

    # ---- 照明 ----
    Equipment("手电筒", "照明"),
    Equipment("油灯", "照明", era=["1920s"]),
    Equipment("蜡烛 (6支)", "照明"),

    # ---- 工具 ----
    Equipment("绳索 (15米)", "工具"),
    Equipment("粉笔", "工具"),
    Equipment("小刀/折叠刀", "工具"),
    Equipment("锁匠工具", "工具", min_credit=20),
    Equipment("撬棍", "工具"),
    Equipment("锤子与钉子", "工具"),
    Equipment("望远镜", "工具", min_credit=20),
    Equipment("指南针", "工具"),
    Equipment("放大镜", "工具", min_credit=10),

    # ---- 医疗 ----
    Equipment("急救箱", "医疗", min_credit=10),
    Equipment("绷带", "医疗"),
    Equipment("吗啡注射剂", "医疗", min_credit=30, era=["1920s"]),

    # ---- 记录/通讯 ----
    Equipment("照相机", "记录/通讯", min_credit=20),
    Equipment("日记本", "记录/通讯"),
    Equipment("钢笔与墨水", "记录/通讯"),
    Equipment("报纸", "记录/通讯"),
    Equipment("地图（本地）", "记录/通讯"),
    Equipment("手机", "记录/通讯", era=["modern"], min_credit=10),

    # ---- 防护 ----
    Equipment("厚皮外套", "防护"),
    Equipment("防弹背心", "防护", min_credit=50, era=["modern"]),
    Equipment("头盔", "防护", min_credit=20),
    Equipment("防毒面具", "防护", min_credit=30),

    # ---- 武器 ----
    Equipment("拐杖/手杖", "武器"),
    Equipment("小型刀具", "武器"),
    Equipment("斧头", "武器", min_credit=10),
    Equipment(".22 口径手枪", "武器", min_credit=20, era=["1920s"]),
    Equipment(".32 口径手枪", "武器", min_credit=30, era=["1920s"]),
    Equipment(".45 口径手枪", "武器", min_credit=40, era=["1920s"]),
    Equipment("9mm 手枪", "武器", min_credit=30, era=["modern"]),
    Equipment("猎枪", "武器", min_credit=30),
    Equipment("步枪", "武器", min_credit=40),

    # ---- 交通 ----
    Equipment("自行车", "交通", min_credit=10),
    Equipment("普通轿车", "交通", min_credit=30),
    Equipment("摩托车", "交通", min_credit=20, era=["modern"]),

    # ---- 特殊 ----
    Equipment("图书馆借阅证", "特殊"),
    Equipment("十字架/宗教圣物", "特殊"),
    Equipment("威士忌酒壶", "特殊"),
    Equipment("烟斗", "特殊"),
]


def get_available_equipment(
    era: str = "1920s", credit_rating: int = 0,
) -> list[dict]:
    era_lower = era.lower().replace(" ", "")
    results = []
    for eq in COC_EQUIPMENT:
        era_match = any(e.lower().replace(" ", "") == era_lower for e in eq.era)
        if era_match and eq.min_credit <= credit_rating:
            results.append(eq.to_dict())
    return results
