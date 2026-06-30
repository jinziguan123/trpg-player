"""CoC 7th 专精技能类别（母语/外语/格斗/射击/科学/生存/技艺/驾驶）。

数据取自克苏鲁公社人物卡生成器；每个专精项带自身起始值 init。
母语固定等于 EDU（运行时按角色 EDU 赋值），外语等专精用各自 init。
"""

SPECIALIZATIONS: dict[str, dict] = {
    "母语": {
        "base_init": 0,
        "items": [
        {"name": "汉语", "init": 0},
        {"name": "英语", "init": 0},
        {"name": "日语", "init": 0},
        {"name": "法语", "init": 0},
        {"name": "俄语", "init": 0},
        {"name": "德语", "init": 0},
        {"name": "韩语", "init": 0},
        {"name": "粤语", "init": 0},
        {"name": "拉丁语", "init": 0},
        {"name": "荷兰语", "init": 0},
        {"name": "挪威语", "init": 0},
        {"name": "丹麦语", "init": 0},
        {"name": "印地语", "init": 0},
        {"name": "西班牙语", "init": 0},
        {"name": "葡萄牙语", "init": 0},
        {"name": "阿拉伯语", "init": 0},
        ],
    },
    "外语": {
        "base_init": 1,
        "items": [
        {"name": "汉语", "init": 1},
        {"name": "英语", "init": 1},
        {"name": "日语", "init": 1},
        {"name": "法语", "init": 1},
        {"name": "俄语", "init": 1},
        {"name": "德语", "init": 1},
        {"name": "韩语", "init": 1},
        {"name": "粤语", "init": 1},
        {"name": "拉丁语", "init": 1},
        {"name": "荷兰语", "init": 1},
        {"name": "挪威语", "init": 1},
        {"name": "丹麦语", "init": 1},
        {"name": "印地语", "init": 1},
        {"name": "西班牙语", "init": 1},
        {"name": "葡萄牙语", "init": 1},
        {"name": "阿拉伯语", "init": 1},
        ],
    },
    "格斗": {
        "base_init": 0,
        "items": [
        {"name": "斗殴", "init": 25},
        {"name": "刀剑", "init": 20},
        {"name": "矛", "init": 20},
        {"name": "斧", "init": 15},
        {"name": "绞索", "init": 15},
        {"name": "链锯", "init": 10},
        {"name": "链枷", "init": 10},
        {"name": "鞭", "init": 5},
        ],
    },
    "射击": {
        "base_init": 0,
        "items": [
        {"name": "手枪", "init": 20},
        {"name": "步/霰", "init": 25},
        {"name": "冲锋枪", "init": 15},
        {"name": "弓弩", "init": 15},
        {"name": "机枪", "init": 10},
        {"name": "重武器", "init": 10},
        ],
    },
    "科学": {
        "base_init": 1,
        "items": [
        {"name": "数学", "init": 10},
        {"name": "物理", "init": 1},
        {"name": "化学", "init": 1},
        {"name": "药学", "init": 1},
        {"name": "地质学", "init": 1},
        {"name": "生物学", "init": 1},
        {"name": "动物学", "init": 1},
        {"name": "植物学", "init": 1},
        {"name": "天文学", "init": 1},
        {"name": "密码学", "init": 1},
        {"name": "气象学", "init": 1},
        {"name": "工程学", "init": 1},
        {"name": "鉴证", "init": 1},
        {"name": "制药", "init": 1},
        ],
    },
    "生存": {
        "base_init": 5,
        "items": [
        {"name": "沙漠", "init": 5},
        {"name": "森林", "init": 5},
        {"name": "荒岛", "init": 5},
        {"name": "高山", "init": 5},
        {"name": "海上", "init": 5},
        ],
    },
    "技艺": {
        "base_init": 5,
        "items": [
        {"name": "表演", "init": 5},
        {"name": "音乐", "init": 5},
        {"name": "绘画", "init": 5},
        {"name": "艺术", "init": 5},
        {"name": "摄影", "init": 5},
        {"name": "写作", "init": 5},
        {"name": "书法", "init": 5},
        {"name": "打字", "init": 5},
        {"name": "速记", "init": 5},
        {"name": "伪造", "init": 5},
        {"name": "烹饪", "init": 5},
        {"name": "裁缝", "init": 5},
        {"name": "理发", "init": 5},
        {"name": "技术制图", "init": 5},
        {"name": "耕作", "init": 5},
        {"name": "木工", "init": 5},
        {"name": "铁匠", "init": 5},
        {"name": "焊接", "init": 5},
        {"name": "管道工", "init": 5},
        ],
    },
    "驾驶": {
        "base_init": 1,
        "items": [
        {"name": "船", "init": 1},
        {"name": "马车", "init": 1},
        {"name": "飞行器", "init": 1},
        ],
    },
}

# 母语唯一（你的本族语言，值=EDU）；其余可有多个专精实例
SINGLE_SPEC = {"母语"}
