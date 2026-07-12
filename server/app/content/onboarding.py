"""新手团使用的项目原创短模组与预设调查员。"""

SAMPLE_SLUG = "first-case-v1"

SAMPLE_MODULE = {
    "title": "雾港失灯事件",
    "rule_system": "coc",
    "description": "调查一座雾港灯塔突然熄灭的原因，适合第一次体验文字跑团。",
    "theme": "default",
    "world_setting": {
        "source": "trpg-player-original",
        "sample_slug": SAMPLE_SLUG,
        "era": "1920s",
        "region": "北海岸",
        "location": "雾港",
        "tone": "悬疑、轻度恐怖",
        "difficulty": "入门",
        "player_count": "1",
        "tags": ["原创", "新手", "调查"],
        "intro": "北海岸的雾港依靠老灯塔指引夜航。今夜，灯火第一次无故熄灭。",
        "player_brief": "你受港务员委托，在下一艘邮船抵达前查明灯塔熄灭的原因。",
    },
    "raw_content": "本模组为 TRPG Player 项目原创新手示例内容。",
    "scenes": [
        {
            "id": "fog_harbor_office",
            "name": "港务所",
            "description": "潮湿的木屋里堆着航海日志，窗外浓雾贴着玻璃流动。",
            "danger": "calm",
            "atmosphere": "焦急、潮湿、钟声遥远",
            "connections": ["old_lighthouse"],
        },
        {
            "id": "old_lighthouse",
            "name": "老灯塔",
            "description": "石阶盘旋向上，灯室里残留盐粒与一道不属于人的湿脚印。",
            "danger": "uneasy",
            "atmosphere": "黑暗、海风、金属摩擦声",
            "connections": ["fog_harbor_office"],
        },
    ],
    "npcs": [
        {
            "id": "harbor_master_lin",
            "name": "林恩港务员",
            "description": "一位眼下乌青、反复核对怀表的中年人。",
            "personality": "务实而紧张，不愿让港口陷入恐慌",
            "background": "负责记录船只进出与灯塔维护。",
            "secrets": ["昨夜听见灯塔方向传来三次短促钟声，却没有写进值班记录。"],
            "initial_location": "fog_harbor_office",
            "skills": {"话术": 45, "心理学": 35},
        }
    ],
    "clues": [
        {
            "id": "torn_log_page",
            "name": "被撕下的航海日志",
            "description": "纸边的新鲜纤维说明这一页刚被撕走，背页留下“退潮后开门”的压痕。",
            "location": "fog_harbor_office",
            "trigger_condition": "调查航海日志并通过侦查检定",
        },
        {
            "id": "salt_footprint",
            "name": "盐渍脚印",
            "description": "脚印从灯室中央开始，没有从门口或窗边延伸而来。",
            "location": "old_lighthouse",
            "trigger_condition": "检查灯室地面",
        },
    ],
    "maps": [],
    "triggers": [],
    "handouts": [],
}

SAMPLE_CHARACTER = {
    "name": "许闻舟",
    "rule_system": "coc",
    "is_player": True,
    "base_attributes": {
        "STR": 50,
        "CON": 55,
        "SIZ": 50,
        "DEX": 60,
        "APP": 50,
        "INT": 70,
        "POW": 60,
        "EDU": 65,
        "LUCK": 55,
    },
    "skills": {
        "侦查": 65,
        "聆听": 55,
        "图书馆使用": 60,
        "心理学": 45,
        "话术": 40,
        "闪避": 30,
    },
    "system_data": {
        "occupation": "记者",
        "hitPoints": {"current": 10, "max": 10},
        "sanity": {"current": 60, "max": 99},
        "magicPoints": {"current": 12, "max": 12},
        "moveRate": 8,
        "build": 0,
        "damageBonus": "0",
        "equipment": ["笔记本", "钢笔", "手电筒", "相机"],
        "weapons": [],
    },
    "backstory": "地方报记者，对无法解释的细节有近乎固执的好奇心。",
}
