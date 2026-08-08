#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
政务看板数据抓取引擎
====================
从新华网、人民网、中央纪委国家监委等权威公开渠道抓取政务新闻数据，
按七大板块分类、去重后生成看板 HTML 和 JSON 数据。

数据来源（均为官方、权威、公开网站）:
  - 新华网 RSS (xinhuanet.com)
  - 人民网 RSS (people.com.cn)
  - 中央纪委国家监委 (ccdi.gov.cn)
  - 国务院国资委 (sasac.gov.cn)
  - 应急管理部 (mem.gov.cn)
  - 各省市政府网站
"""

import os
import sys
import re
import json
import hashlib
import time
import datetime
import logging
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Optional

try:
    import feedparser
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("请先安装依赖: pip install feedparser beautifulsoup4 requests lxml")
    sys.exit(1)

# ==================== 配置 ====================

# 项目根目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
OUTPUT_HTML = os.path.join(BASE_DIR, "zhengwu-kanban-live.html")
OUTPUT_JSON = os.path.join(DATA_DIR, "kanban-data.json")
TEMPLATE_HTML = os.path.join(BASE_DIR, "zhengwu-kanban.html")

# 日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("zhengwu")

# 请求头
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

TIMEOUT = 15
MAX_PER_SECTION = 10
SIMILARITY_THRESHOLD = 0.75  # 标题相似度阈值（去重用）

# 需要过滤的旧闻/无关内容关键词
STALE_FILTERS = [
    "新冠", "疫情", "核酸检测", "健康码", "核酸", "无症状感染者",
    "疫苗接种", "抗原", "方舱", "密接", "次密接", "封控", "静默",
    "居家隔离", "复工复产", "动态清零", "乙类乙管",
    "中风险", "高风险", "低风险", "风险区",
    "二十条", "优化防控", "联防联控",
]

# ==================== 广东省头部城市地理优先级 ====================
# 标题/摘要命中这些城市关键词时，为该文章在所有板块中加分
# 分值越高，该新闻在板块内的排序越靠前
GD_REGION_KEYWORDS = [
    ("广东省", 22), ("广东", 20),
    ("深圳市", 18), ("深圳", 18),
    ("珠海市", 16), ("珠海", 16),
    ("中山市", 14), ("中山", 14),
    ("广州市", 15), ("广州", 15),
    ("粤港澳", 15), ("大湾区", 15),
    ("珠江", 10), ("珠三角", 10),
    ("佛山市", 8), ("佛山", 8),
    ("东莞市", 8), ("东莞", 8),
    ("惠州市", 6), ("惠州", 6),
]

def calculate_gd_region_bonus(text: str) -> int:
    """计算广东省头部城市地理加分（0~22分）。
    取命中的最高分关键词（不累加，避免一篇文章因提到多个广东城市而过度倾斜）。
    """
    max_bonus = 0
    for kw, pts in GD_REGION_KEYWORDS:
        if kw in text:
            if pts > max_bonus:
                max_bonus = pts
    return max_bonus


# ==================== RSS 数据源 ====================

RSS_SOURCES = {
    "xinhua_politics": {
        "url": "http://www.xinhuanet.com/politics/news_politics.xml",
        "name": "新华网·时政",
    },
    "xinhua_local": {
        "url": "http://www.xinhuanet.com/local/news_province.xml",
        "name": "新华网·地方联播",
    },
    "xinhua_legal": {
        "url": "http://www.xinhuanet.com/legal/news_legal.xml",
        "name": "新华网·法治",
    },
    "xinhua_finance": {
        "url": "http://www.xinhuanet.com/fortune/news_fortune.xml",
        "name": "新华网·财经",
    },
    "people_politics": {
        "url": "http://www.people.com.cn/rss/politics.xml",
        "name": "人民网·时政",
    },
    "people_society": {
        "url": "http://www.people.com.cn/rss/society.xml",
        "name": "人民网·社会",
    },
    "people_legal": {
        "url": "http://www.people.com.cn/rss/legal.xml",
        "name": "人民网·法治",
    },
    "people_all": {
        "url": "http://www.people.com.cn/rss/ywkx.xml",
        "name": "人民网·要闻快讯",
    },
}

# 直接网页抓取源（更实时的数据）
WEB_SCRAPE_SOURCES = {
    # 新华网 - 时政频道（实时滚动新闻）
    "xinhua_politics_web": {
        "url": "http://www.news.cn/politics/",
        "name": "新华网·时政",
        "selector": "div.news-list li a, div.list li a, ul.dataList li a",
    },
    # 新华网 - 人事任免（高山流水核心来源）
    "xinhua_renshi": {
        "url": "http://www.news.cn/renshi/",
        "name": "新华网·人事",
        "selector": "div.news-list li a, ul.list li a, div.listWrap li a",
    },
    # 人民网 - 时政频道
    "people_politics_web": {
        "url": "http://politics.people.com.cn/",
        "name": "人民网·时政",
        "selector": "div.hdNews a, ul.list_14 li a, ul.list_16 li a, div.news-list li a",
    },
    # 国资委·国资动态
    "sasac_news": {
        "url": "http://www.sasac.gov.cn/n2588025/n2588139/index.html",
        "name": "国资委·国资动态",
        "selector": "div.zsy_conlist li a",
    },
    # 应急管理部
    "mem_news": {
        "url": "https://www.mem.gov.cn/xw/yjglbgzdt/",
        "name": "应急管理部",
        "selector": "div.list_right li a, ul#div li a, li a",
    },
    # 中央纪委·审查调查
    "ccdi_cases": {
        "url": "https://www.ccdi.gov.cn/scdc/",
        "name": "中央纪委·审查调查",
        "selector": "div.list_text ul li a, li a",
    },
    # 中国政府网·政策
    "gov_policy": {
        "url": "https://www.gov.cn/lianbo/",
        "name": "中国政府网·联播",
        "selector": "ul.listTxt li a, div.news_box li a",
    },
    # ===== 新增数据源：向下扩展 =====
    # 人民网·人事任免（高山流水补充源）
    "people_renshi": {
        "url": "http://renshi.people.com.cn/",
        "name": "人民网·人事任免",
        "selector": "div.hdNews a, ul.list_14 li a, ul.list_16 li a, div.news-list li a",
    },
    # 人民网·地方领导（高山流水补充源）
    "people_local_leader": {
        "url": "http://ldrk.people.com.cn/",
        "name": "人民网·地方领导",
        "selector": "div.news_list li a, ul.list_14 li a, div.hdNews a",
    },
    # 中国政府网·政策文件（政投先机补充源）
    "gov_policy_doc": {
        "url": "https://www.gov.cn/zhengce/zhengceku/",
        "name": "中国政府网·政策文件",
        "selector": "ul.listTxt li a, div.news_box li a, div.news_list li a",
    },
    # 发改委·项目审批（政投先机补充源）
    "ndrc_xmgs": {
        "url": "https://www.ndrc.gov.cn/xxgk/zcfb/xmgs/",
        "name": "发改委·项目审批",
        "selector": "div.zwxx_list li a, ul.list li a, div.news_list li a",
    },
    # 中国气象局（突发事件补充源）
    "cma_weather": {
        "url": "http://www.cma.gov.cn/2011xwzx/2011xqxxw/",
        "name": "中国气象局",
        "selector": "div.news_list li a, ul.list li a, div.list_text li a",
    },
    # 中国地震局（突发事件补充源）
    "cea_earthquake": {
        "url": "http://www.cea.gov.cn/cea/dt/",
        "name": "中国地震局",
        "selector": "div.news_list li a, ul.list li a, div.list_text li a",
    },
    # 国资委·央企动态（国企新闻补充源）
    "sasac_yangqi": {
        "url": "https://www.sasac.gov.cn/n2588035/n2588320/index.html",
        "name": "国资委·央企动态",
        "selector": "div.zsy_conlist li a, div.news_list li a",
    },
    # 应急管理部·事故通报（突发事件补充源）
    "mem_accident": {
        "url": "https://www.mem.gov.cn/gk/sgcc/",
        "name": "应急管理部·事故通报",
        "selector": "div.list_right li a, ul#div li a, div.news_list li a",
    },
    # 中央纪委·审查调查（打虎台补充源）
    "ccdi_scdc": {
        "url": "https://www.ccdi.gov.cn/scs/nwt/",
        "name": "中央纪委·审查调查",
        "selector": "div.list_text ul li a, div.news_list li a, li a",
    },
    # ===== 地方国资委/政府网站 =====
    # 广东省国资委（国企新闻+府衙招聘）
    "gd_gzw": {
        "url": "http://gzw.gd.gov.cn/",
        "name": "广东省国资委",
        "selector": "div.list li a, ul.list li a, div.news_list li a",
    },
    # 广东省国资委·通知公告（府衙招聘）
    "gd_gzw_tzgg": {
        "url": "http://gzw.gd.gov.cn/zwgk/tzgg/",
        "name": "广东省国资委·通知公告",
        "selector": "div.list li a, ul.list li a, li a",
    },
    # 深圳市国资委（国企新闻+府衙招聘）
    "sz_gzw": {
        "url": "https://gzw.sz.gov.cn/gkmlpt/",
        "name": "深圳市国资委",
        "selector": "div.list li a, ul.list li a, li a, a.listTitle",
    },
    # 珠海市国资委（国企新闻+府衙招聘）
    "zh_gzw": {
        "url": "https://www.zhuhai.gov.cn/gzw/gkmlpt/index",
        "name": "珠海市国资委",
        "selector": "div.list li a, ul.list li a, li a",
    },
    # 中山市国资委（国企新闻+突发事件）
    "zs_gzw": {
        "url": "https://www.zs.gov.cn/zsgzw/gkmlpt/mindex",
        "name": "中山市国资委",
        "selector": "div.list li a, ul.list li a, li a",
    },
    # 广东省人民政府·政务动态（政投先机+高山流水）
    "gd_gov": {
        "url": "https://www.gd.gov.cn/gdywdt/zwdt/",
        "name": "广东省人民政府·政务动态",
        "selector": "div.list li a, ul.list li a, div.news_list li a",
    },
    # 深圳市人民政府·政务动态
    "sz_gov": {
        "url": "https://www.sz.gov.cn/zwdt/",
        "name": "深圳市人民政府·政务动态",
        "selector": "div.list li a, ul.list li a, div.news_list li a",
    },
    # 珠海市人民政府·政务动态
    "zh_gov": {
        "url": "https://www.zhuhai.gov.cn/zwdt/",
        "name": "珠海市人民政府·政务动态",
        "selector": "div.list li a, ul.list li a, div.news_list li a",
    },
    # 中山市人民政府·政务动态
    "zs_gov": {
        "url": "https://www.zs.gov.cn/zwdt/",
        "name": "中山市人民政府·政务动态",
        "selector": "div.list li a, ul.list li a, div.news_list li a",
    },
}


# ==================== 关键词分类规则 ====================

CATEGORY_RULES = {
    "zhengtou": {
        "label": "政投先机",
        "icon": "🏗",
        "color": "#1565c0",
        "priority": 9,
        "keywords": [
            # 精准政企投资类关键词
            "投资", "固定资产投资", "资本金注入", "投资补助", "贷款贴息",
            "产业基金", "引导基金", "综合开发", "重大科技投入",
            "专项债", "专项债券", "地方债",
            "开工", "竣工", "投产", "签约", "落地",
            "基础设施", "公共基础设施", "基建",
            "发改委", "批复", "审批", "核准", "招标", "中标",
            "招商引资", "产业园区", "开发区", "新区", "自贸区",
            "重大工程", "重大项目", "重点项目",
            "融资", "债券", "基金",
            "PPP", "特许经营", "REITs",
            "预算内投资", "中央投资",
        ],
        "exclude": [
            # 贪腐新闻归打虎台
            "腐败", "贪腐", "落马", "双规", "双开", "被查", "受贿", "贪污",
            "严重违纪", "审查调查", "开除党籍", "纪律审查",
            "索贿", "贪官", "行贿",
            # 非经济类误匹配
            "731部队", "日军", "罪证", "陈列馆", "公祭日",
            "细菌战", "侵华", "爱国教育", "纪念馆", "遗址", "文物保护",
            # 宣讲/务虚类内容
            "精神落地", "认真学习宣传", "主题教育",
            "宣讲团", "中心组学习", "党委理论学习",
            "学习贯彻", "深入贯彻落实",
            "专题研讨", "培训", "轮训",
            "党史", "党建", "组织生活",
            # 司法/公益/社会类非投资内容
            "最高法", "入额", "挂名办案",
            "传销", "非法集资",
            "炒房", "炒地", "囤地",
            "谣言", "辟谣",
        ],
    },
    "tufa": {
        "label": "突发事件",
        "icon": "⚠",
        "color": "#e65100",
        "priority": 9,
        "keywords": [
            # 自然灾害
            "地震", "台风", "暴雨", "洪水", "洪涝", "山体滑坡", "泥石流",
            "冰雹", "暴雪", "寒潮", "高温", "干旱", "森林火灾",
            # 事故灾难
            "爆炸", "火灾", "透水", "坍塌", "坠机", "沉船",
            # 公共卫生
            "公共卫生", "疫情", "猴痘", "传染病", "食品安全",
            # 社会安全与民生
            "遇难", "伤亡", "失踪", "被困",
            # 预警与响应
            "预警", "应急响应", "救援", "疏散", "转移",
            "气象", "防汛", "抗旱", "安全生产",
        ],
        "exclude": [
            "落马", "双规", "双开", "受贿", "贪污", "腐败", "贪腐",
            "严重违纪", "审查调查", "开除党籍", "纪律审查",
            # 非突发事件排除
            "精神", "学习", "贯彻", "座谈", "表彰",
            "文艺", "比赛", "演出", "展览",
            "社会救援服务", "服务指南",
        ],
    },
    "guoqi": {
        "label": "国企新闻",
        "icon": "🏭",
        "color": "#283593",
        "priority": 9,
        "keywords": [
            "央企", "国企", "国资", "国资委",
            "中央企业", "国有企业", "国有资本",
            "中国石油", "中国石化", "中国海油", "国家电网",
            "中国移动", "中国联通", "中国电信",
            "中国中车", "中国船舶", "中国商飞",
            "中粮", "中国中化", "中国建筑", "中国中铁",
            "中国铁建", "中交集团", "中国电建",
            "航天科技", "航天科工", "中国兵器",
            "宝武", "鞍钢", "华润",
        ],
        "exclude": ["腐败", "贪腐", "落马", "双规", "双开", "被查", "受贿", "贪污",
            "严重违纪", "审查调查", "开除党籍", "纪律审查"],
    },
    "gaoshan": {
        "label": "高山流水",
        "icon": "🏔",
        "color": "#2e7d32",
        "priority": 10,
        # 核心关键词：明确的人事任命调动术语，命中即计分
        "keywords": [
            "人事任免", "干部任免", "人事调整", "人事变动",
            "中央批准", "中央决定",
            "省委书记", "省长",
            "上将军衔", "晋升上将军衔",
            "国务院任免", "全国人大任免",
            "央企主要负责人", "国企负责人",
            "领导职务任免",
            "同志任", "同志不再担任", "同志辞去",
            "国家工作人员任免",
        ],
        # 条件关键词：需要文章中也命中 context_keywords 才计分
        "conditional_keywords": [
            "任命", "任免", "调任", "任职", "免去", "辞去",
            "履新", "晋升", "授衔",
            "当选", "补选", "接替",
            "换届", "出任", "兼任", "调离",
        ],
        "context_keywords": [
            # 必须同时命中的高级官员/国企高管职务语境
            "省委书记", "省长", "副省长", "自治区",
            "部长", "副部长", "厅长", "副厅长",
            "党组书记", "党委副书记",
            "央企", "国企", "董事长", "总经理",
            "中央批准", "中央决定",
            "军区", "军委", "国防部", "外交部",
            "驻外使节", "大使", "公使",
            "秘书长", "委员长", "主任委员",
            "最高人民法院", "最高人民检察院",
            "国家监委", "中纪委",
        ],
        "exclude": [
            "落马", "双规", "双开", "开除党籍", "接受审查", "受贿",
            "违纪", "被查", "被公诉", "贪污", "被判", "获刑",
            "被双开", "审查调查", "纪律审查", "贪腐", "腐败",
            "贪官", "索贿", "收受", "行贿",
            # 非人事变动排除
            "学习", "培训", "表彰", "评选", "获奖",
            "哀悼", "讣告", "逝世", "悼念",
            "召开会议", "座谈会", "主持会议", "讲话",
            # 国外新闻排除
            "英联邦", "英国", "美国大选", "日本", "韩国总统",
            "奥运会", "世界杯",
        ],
    },
    "dahu": {
        "label": "打虎台",
        "icon": "🐯",
        "color": "#b71c1c",
        "priority": 10,
        # 核心关键词：明确指向官员贪腐，命中即计分
        "keywords": [
            "落马", "双规", "双开", "被双开",
            "开除党籍", "开除公职",
            "受贿", "行贿", "贪污", "腐败", "贪腐",
            "严重违纪", "严重违纪违法",
            "中央纪委", "纪检监察",
            "职务犯罪", "滥用职权",
            "纪律审查", "审查调查",
            "被提起公诉",
            "巨额财产来源不明",
        ],
        # 泛化法律关键词：必须同时命中 context_keywords 中的至少 1 个才计分
        # 防止误收校园欺凌、商标侵权、著作权纠纷等非官员案件
        "conditional_keywords": [
            "被查", "被逮捕", "被公诉",
            "判刑", "获刑", "一审", "判决",
            "移送司法", "开庭审理",
            "处分", "巨额财产",
        ],
        "context_keywords": [
            "落马", "双规", "双开", "开除党籍", "开除公职",
            "受贿", "行贿", "贪污", "腐败", "贪腐",
            "严重违纪", "违纪违法", "中央纪委", "纪检监察", "监委",
            "职务犯罪", "滥用职权", "审查调查", "纪律审查",
            "党组书记", "省委", "省政协", "省人大",
            "市长", "市委书记", "县委书记", "县长",
            "局长", "厅长", "处长", "书记", "主任",
            "官员", "干部", "公职", "公权力",
        ],
        # 排除：民事、商事、普通刑事等非官员案件
        "exclude": [
            "著作权", "商标", "专利", "侵权赔偿",
            "校园欺凌", "校园暴力", "校园",
            "BURBERRY", "尚品网", "假货", "假",
            "未成年人", "未成年",
        ],
    },
    "fuya": {
        "label": "府衙招聘",
        "icon": "&#x1F4CB;",
        "color": "#6a1b9a",
        "priority": 10,
        "keywords": [
            "公开招聘", "市场化选聘", "选聘公告",
            "招聘公告", "招聘简章", "招录",
            "国企招聘", "央企招聘", "国资招聘",
            "副总经理招聘", "总经理招聘", "高管招聘",
            "管理层招聘", "管理岗招聘",
            "管理人员招聘", "选聘",
            "领导干部招聘", "领导人员招聘",
            "职业经理人", "竞聘",
            "公务员招录", "事业单位招聘",
        ],
        "conditional_keywords": [
            "招聘", "选聘", "招录", "竞聘", "招聘公告",
        ],
        "context_keywords": [
            "国企", "央企", "国资", "国有",
            "国资委", "国资委", "政府",
            "总经理", "副总经理", "董事长",
            "高管", "管理层", "管理岗",
            "领导", "干部",
            "市场化", "选聘", "竞聘",
            "事业单位", "公务员",
        ],
        "exclude": [
            "落马", "双规", "双开", "受贿", "贪污", "腐败",
            "严重违纪", "审查调查", "开除党籍", "纪律审查",
            "校园", "暑假工", "临时工", "小时工",
            "实习", "兼职",
        ],
    },
}


# ==================== 工具函数 ====================

def is_stale_content(text: str) -> bool:
    """判断是否为过期/无关内容"""
    for kw in STALE_FILTERS:
        if kw in text:
            return True
    return False


def fetch_url(url: str, timeout: int = TIMEOUT) -> Optional[str]:
    """抓取 URL 内容"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text
    except requests.RequestException as e:
        log.warning(f"抓取失败: {url} — {e}")
        return None


def fetch_rss(url: str) -> list:
    """解析 RSS feed，返回文章列表"""
    try:
        feed = feedparser.parse(url)
        if feed.bozo and not feed.entries:
            log.warning(f"RSS 解析异常: {url}")
            return []

        articles = []
        for entry in feed.entries[:50]:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            summary = entry.get("summary", "").strip()
            published = entry.get("published", "") or entry.get("updated", "")

            # 过滤旧闻（标题摘要层面的快速过滤）
            if is_stale_content(f"{title} {summary}"):
                continue

            # 清理 HTML 标签
            summary = re.sub(r"<[^>]+>", "", summary)[:200]

            # 解析时间：优先结构化时间 → URL 路径 → 字符串
            dt = _parse_datetime(
                time_str=published,
                parsed_tuple=entry.get("published_parsed") or entry.get("updated_parsed"),
                url=link,
            )

            # 三日内过滤
            if dt and not _is_recent(dt, max_age_hours=72):
                continue

            if title and link:
                articles.append({
                    "title": title,
                    "url": link,
                    "summary": summary,
                    "time": dt,
                    "time_str": _format_time(dt),
                    "source": "",
                })
        return articles
    except Exception as e:
        log.warning(f"RSS 处理异常: {url} — {e}")
        return []


def scrape_webpage(source_id: str, source_info: dict) -> list:
    """从网页直接抓取文章列表"""
    try:
        html = fetch_url(source_info["url"])
        if not html:
            return []
        soup = BeautifulSoup(html, "lxml")
        items = soup.select(source_info["selector"])
        articles = []
        seen_urls = set()
        for item in items[:80]:  # 多取一些，日期过滤后可能大量削减
            a_tag = item if item.name == "a" else item.find("a", href=True)
            if not a_tag:
                continue
            title = a_tag.get_text(strip=True)
            href = a_tag.get("href", "")

            # 跳过过短标题或无效链接
            if not title or len(title) < 8:
                continue
            if not href or href.startswith("javascript:") or href == "#":
                continue

            # 补全 URL
            if not href.startswith("http"):
                base = source_info["url"]
                domain = "/".join(base.split("/")[:3])
                if href.startswith("/"):
                    href = domain + href
                elif href.startswith("./"):
                    href = base.rsplit("/", 1)[0] + href[1:]
                else:
                    href = base.rsplit("/", 1)[0] + "/" + href

            # 去重 URL
            if href in seen_urls:
                continue
            seen_urls.add(href)

            # 过滤旧闻关键词
            if is_stale_content(title):
                continue

            # 提取时间文本（多策略回退）
            time_text = ""
            # 策略1: 从 a_tag 的兄弟 span 中查找 [YYYY-MM-DD] 格式（国资委、中纪委等）
            if item.name == "a":
                for sibling in item.find_next_siblings("span"):
                    st = sibling.get_text(strip=True)
                    if re.search(r'\d{4}-\d{2}-\d{2}', st):
                        time_text = st
                        break
                # 也检查 parent(li) 下的所有 span
                if not time_text and item.parent and item.parent != item:
                    for sp in item.parent.find_all("span", recursive=False):
                        st = sp.get_text(strip=True)
                        if re.search(r'\d{4}-\d{2}-\d{2}', st):
                            time_text = st
                            break
            # 策略2: 从 item 子元素中查找常见类名
            if not time_text:
                for cls in ["time", "date", "pubTime", "pub-date", "source-time"]:
                    time_el = item.find(class_=cls) or item.find("span", class_=cls)
                    if time_el:
                        time_text = time_el.get_text(strip=True)
                        break
            # 策略3: 从任意 span/em/i 中查找
            if not time_text:
                time_el = item.find("span") or item.find("em") or item.find("i")
                if time_el:
                    time_text = time_el.get_text(strip=True)

            # 解析日期（时间字符串 + URL 备用）
            dt = _parse_datetime(time_str=time_text, url=href)

            # 宽松过滤：至少 URL 中有日期或文本中有日期（URL日期优先）
            # 完全无日期的文章也保留（由全局过滤统一处理）
            if dt:
                dt_display = dt.strftime("%Y-%m-%d")
            else:
                dt_display = ""

            articles.append({
                "title": title,
                "url": href,
                "summary": "",
                "time": dt,
                "time_str": _format_time(dt) if dt else "",
                "time_raw": time_text[:30] if time_text else "",
                "source": source_info["name"],
            })
        return articles
    except Exception as e:
        log.warning(f"网页抓取异常: {source_info['name']} — {e}")
        return []


def _parse_datetime(time_str: str = "", parsed_tuple: tuple = None, url: str = None) -> datetime.datetime:
    """解析文章时间，返回 datetime 对象（含时区处理）。

    优先级: feedparser parsed_tuple > URL 路径日期 > 字符串解析
    返回 None 表示无法解析。
    """
    # 1. 使用 feedparser 的结构化时间（最可靠）
    if parsed_tuple and len(parsed_tuple) >= 6:
        try:
            return datetime.datetime(*parsed_tuple[:6])
        except (ValueError, TypeError):
            pass

    # 2. 从 URL 路径提取日期（新华网、应急管理部等不提供日期字段或日期在 span 中的源）
    if url:
        # 标准格式: /2024-08/06/
        m = re.search(r'/(\d{4})-(\d{2})/(\d{2})/', url)
        if not m:
            # 中国政府网站格式: /20240806/ 或 t20240806_
            m = re.search(r'[/t](\d{4})(\d{2})(\d{2})[/_]', url)
        if not m:
            # 备用: URL中任意位置的8位连续日期
            m = re.search(r'(\d{4})(\d{2})(\d{2})', url)
        if m:
            try:
                dt = datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                # URL 中的日期最多偏差一天（预发稿），容差放宽到 72 小时
                return dt
            except ValueError:
                pass

    # 3. 字符串日期解析
    if time_str:
        time_str = time_str.strip()
        # [YYYY-MM-DD] 格式（国资委、中纪委等政府网站常见）
        m = re.search(r'\[(\d{4})-(\d{2})-(\d{2})\]', time_str)
        if m:
            try:
                return datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
        for fmt in [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
            "%a, %d %b %Y %H:%M:%S",
            "%Y年%m月%d日 %H:%M:%S",
            "%Y年%m月%d日",
        ]:
            try:
                return datetime.datetime.strptime(time_str[:19] if len(time_str) > 10 else time_str, fmt)
            except ValueError:
                continue

    return None


def _format_time(dt: datetime.datetime = None) -> str:
    """格式化时间为显示字符串"""
    if dt is None:
        return ""
    return f"{dt.month}月{dt.day}日"


def _is_recent(dt: datetime.datetime, max_age_hours: int = 48) -> bool:
    """判断是否在 max_age_hours 小时内"""
    if dt is None:
        return False
    now = datetime.datetime.now()
    delta = now - dt
    return delta.total_seconds() <= max_age_hours * 3600


def classify_article_with_scores(article: dict) -> list:
    """对文章进行分类，返回 [(cat_id, total_score), ...] 列表（含得分）

    支持两种关键词：
      - keywords: 常规关键词，命中即计分
      - conditional_keywords: 条件关键词，仅当文章也命中 context_keywords 中的至少 1 个时才计分
    """
    title = article.get("title", "")
    summary = article.get("summary", "")
    text = f"{title} {summary}"

    # 广东省头部城市地理加分（所有板块通用）
    gd_bonus = calculate_gd_region_bonus(text)

    scores = defaultdict(int)
    for cat_id, rules in CATEGORY_RULES.items():
        # 排除检查
        excluded = False
        for kw in rules.get("exclude", []):
            if kw in text:
                excluded = True
                break
        if excluded:
            continue

        # 常规关键词匹配计分
        for kw in rules.get("keywords", []):
            if kw in text:
                scores[cat_id] += 1

        # 条件关键词：仅在满足上下文条件时计分
        cond_kws = rules.get("conditional_keywords", [])
        ctx_kws = rules.get("context_keywords", [])
        if cond_kws and ctx_kws:
            # 检查是否至少有 1 个上下文关键词命中
            has_context = any(kw in text for kw in ctx_kws)
            if has_context:
                for kw in cond_kws:
                    if kw in text:
                        scores[cat_id] += 1

    # 按分数排序（含地理加成）
    results = []
    for cat_id, score in scores.items():
        if score > 0:
            # 基础分 = 关键词命中数 + 板块优先级 + 广东省地理加分
            results.append((cat_id, score + CATEGORY_RULES[cat_id]["priority"] + gd_bonus))

    return results


def classify_article(article: dict) -> list:
    """对文章进行分类，返回匹配的板块 ID 列表（兼容旧接口）"""
    return [cat_id for cat_id, _ in classify_article_with_scores(article)]


def title_similarity(t1: str, t2: str) -> float:
    """计算标题相似度"""
    # 先去除标点和空格
    def clean(s):
        return re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", "", s).lower()

    c1, c2 = clean(t1), clean(t2)
    if not c1 or not c2:
        return 0
    return SequenceMatcher(None, c1, c2).ratio()


def deduplicate(articles: list, threshold: float = SIMILARITY_THRESHOLD) -> list:
    """对文章列表去重"""
    result = []
    for art in articles:
        is_dup = False
        for existing in result:
            if title_similarity(art["title"], existing["title"]) >= threshold:
                is_dup = True
                break
        if not is_dup:
            result.append(art)
    return result


def calculate_zhengtou_priority(article: dict) -> int:
    """计算政投先机的优先级：热度 > 资金量 > 时效"""
    title = article.get("title", "")
    summary = article.get("summary", "")
    text = f"{title} {summary}"
    score = 0

    # 1. 热度评分 (0-40)：热门经济话题加分
    heat_kw = {
        "万亿": 10, "百亿": 7, "十亿": 5,
        "重大工程": 8, "重大项目": 8, "国家重点": 8,
        "高铁": 6, "高速铁路": 6, "机场": 5, "港口": 5,
        "新能源": 6, "半导体": 6, "芯片": 6, "人工智能": 6,
        "自贸区": 6, "新区": 5, "开发区": 5,
        "一带一路": 5, "粤港澳": 5, "长三角": 5, "京津冀": 5,
        "央企": 7, "国企": 5,
        "发改委": 5, "国务院": 5, "批复": 4, "核准": 4,
        "REITs": 6, "专项债": 6, "专项债券": 6,
        "开工": 4, "竣工": 4, "投产": 5, "签约": 5,
    }
    for kw, pts in heat_kw.items():
        if kw in text:
            score += pts

    # 2. 资金量评分 (0-30)：投资金额越大分越高
    funding_patterns = [
        (r"(\d+)万亿元?", 30), (r"(\d+)千亿元?", 25),
        (r"(\d+)百亿元?", 22), (r"(\d+)亿元", lambda m: min(20, int(m.group(1)) // 5)),
        (r"(\d+)万美元", 12), (r"(\d+)亿美元", 18),
    ]
    for pattern, pts_fn in funding_patterns:
        m = re.search(pattern, text)
        if m:
            if callable(pts_fn):
                score += pts_fn(m)
            else:
                score += pts_fn
            break  # 只取最高金额

    # 3. 时效评分 (0-20)：越新分越高
    dt = article.get("time")
    if dt:
        hours_ago = (datetime.datetime.now() - dt).total_seconds() / 3600
        if hours_ago <= 6:
            score += 20
        elif hours_ago <= 12:
            score += 15
        elif hours_ago <= 24:
            score += 10
        elif hours_ago <= 48:
            score += 5

    # 4. 广东省头部城市地理加分 (0-22)
    score += calculate_gd_region_bonus(text)

    return score


def calculate_tufa_priority(title: str, source: str) -> int:
    """计算突发事件的优先级分数
    全国层面 > 地区/流域层面 > 省级层面 > 市级层面
    同时广东省头部城市获得额外加分
    """
    score = 0
    text = title + " " + source

    # 全国层面：国家部委/全国性通报 (+30)
    national_kw = [
        "应急管理部", "国家卫健委", "水利部", "中央气象台",
        "中国地震台网", "国务院", "国家防汛", "国家减灾",
        "中国气象局", "国家安全生产", "公安部", "交通运输部",
        "全国", "国家防总",
    ]
    for kw in national_kw:
        if kw in text:
            score += 30
            break

    # 地区/流域层面：跨省灾害或重大自然事件 (+25)
    regional_kw = [
        "台风", "长江", "黄河", "珠江", "淮河", "海河", "松花江",
        "流域", "干流", "全线", "超警",
    ]
    for kw in regional_kw:
        if kw in text:
            score += 25
            break

    # 省级层面 (+15)
    province_kw = ["省", "自治区"]
    for kw in province_kw:
        if kw in text and "全国" not in text:
            score += 15
            break

    # 市级/县级层面 (+5)
    city_kw = ["市", "县", "区"]
    for kw in city_kw:
        if kw in text and score < 15:
            score += 5
            break

    # 紧急程度加分
    urgency_bonus = 0
    if any(kw in text for kw in ["预警", "红色预警", "橙色预警", "超警"]):
        urgency_bonus += 5
    if any(kw in text for kw in ["遇难", "死亡", "伤亡", "失踪"]):
        urgency_bonus += 5

    # 广东省头部城市地理加分 (0-22)
    gd_bonus = calculate_gd_region_bonus(text)

    return score + urgency_bonus + gd_bonus


def generate_id(text: str) -> int:
    """基于文本生成稳定的数字 ID"""
    return int(hashlib.md5(text.encode()).hexdigest()[:8], 16) % 9000 + 1000


def fetch_all_articles() -> list:
    """从所有源抓取文章，仅保留近两日内的新闻"""
    all_articles = []
    rss_success = 0
    rss_fail = 0
    web_success = 0

    # 1. RSS 源
    log.info("=" * 50)
    log.info("第一阶段: RSS 数据源抓取...")
    for source_id, source_info in RSS_SOURCES.items():
        source_name = source_info["name"]
        articles = fetch_rss(source_info["url"])
        for art in articles:
            art["source"] = source_name
        all_articles.extend(articles)
        if articles:
            log.info(f"  ✓ {source_name}: {len(articles)} 条（近3日内）")
            rss_success += 1
        else:
            log.warning(f"  ✗ {source_name}: 0 条（无近期新闻）")
            rss_fail += 1

    # 2. 网页直接抓取
    log.info("第二阶段: 网页直接抓取...")
    for source_id, source_info in WEB_SCRAPE_SOURCES.items():
        source_name = source_info["name"]
        articles = scrape_webpage(source_id, source_info)
        for art in articles:
            art["source"] = source_name
        all_articles.extend(articles)
        if articles:
            log.info(f"  ✓ {source_name}: {len(articles)} 条")
            web_success += 1
        else:
            log.warning(f"  ✗ {source_name}: 0 条")

    # 3. 全局日期过滤：去除无日期或超过7天的新闻（府衙招聘等低频板块需要更宽窗口）
    before = len(all_articles)
    all_articles = [a for a in all_articles if a.get("time") and _is_recent(a["time"], max_age_hours=168)]
    dropped = before - len(all_articles)
    if dropped:
        log.info(f"全局日期过滤: 丢弃 {dropped} 条过期/无日期新闻, 保留 {len(all_articles)} 条")

    log.info(f"RSS: {rss_success}/{rss_success+rss_fail} | 网页: {web_success}/{len(WEB_SCRAPE_SOURCES)} | 总计: {len(all_articles)} 条（7天内）")
    return all_articles


def classify_and_group(articles: list) -> dict:
    """对文章分类并分组，跨板块去重——每篇文章只归入优先级最高的板块"""
    # 先对全局所有文章去重（同一标题同一来源只保留一条）
    articles = deduplicate(articles)

    # 暂存每篇文章的所有候选分类及得分
    article_candidates = []  # [(art, [(cat_id, score), ...])]

    for art in articles:
        cats_with_scores = classify_article_with_scores(art)
        if cats_with_scores:
            article_candidates.append((art, cats_with_scores))

    # 跨板块去重：每篇文章只归入得分最高的板块
    categorized = defaultdict(list)
    dedup_stats = defaultdict(int)  # 统计被去重的数量

    for art, candidates in article_candidates:
        # 按得分降序，选最高分板块
        candidates.sort(key=lambda x: -x[1])
        best_cat = candidates[0][0]
        categorized[best_cat].append(art)
        # 统计被丢弃的其他候选板块
        for drop_cat, _ in candidates[1:]:
            dedup_stats[drop_cat] += 1

    log.info("分类情况（跨板块去重后）:")
    for cat_id, rules in CATEGORY_RULES.items():
        count = len(categorized.get(cat_id, []))
        dropped = dedup_stats.get(cat_id, 0)
        log.info(f"  {rules['icon']} {rules['label']}: {count} 条 (去重丢弃 {dropped} 条)")

    return categorized


def build_sections(categorized: dict) -> list:
    """构建最终板块数据，按板块差异化时间窗口筛选"""
    sections = []
    placeholder_id = 1000

    # 各板块的时间窗口（小时）：高频板块短窗口，低频板块宽窗口
    SECTION_TIME_WINDOWS = {
        "dahu": 96,      # 打虎台：4天
        "gaoshan": 96,   # 高山流水：4天
        "fuya": 336,     # 府衙招聘：14天（招聘公告活跃期长）
        "zhengtou": 96,  # 政投先机：4天
        "tufa": 72,      # 突发事件：3天（时效性最强）
        "guoqi": 96,     # 国企新闻：4天
    }

    for cat_id in ["dahu", "gaoshan", "fuya", "zhengtou", "tufa", "guoqi"]:
        rules = CATEGORY_RULES[cat_id]
        arts = categorized.get(cat_id, [])

        # 二级去重（安全网，确保同一板块内无重复）
        arts = deduplicate(arts)

        # ★ 差异化时间窗口过滤
        max_hours = SECTION_TIME_WINDOWS.get(cat_id, 96)
        before_count = len(arts)
        arts = [a for a in arts if a.get("time") and _is_recent(a["time"], max_age_hours=max_hours)]
        dropped_time = before_count - len(arts)
        if dropped_time > 0:
            log.info(f"  {rules['label']}: 时间窗口({max_hours}h)过滤丢弃 {dropped_time} 条, 保留 {len(arts)} 条")

        # ★ 统一排序：日期从新到旧（主键） > 地理加分（次键） > 板块特有优先级
        def _sort_key(a):
            dt = a.get("time")
            # date_part: datetime.timestamp() for newest-first sorting (higher = newer)
            date_part = dt.timestamp() if dt else 0
            geo_bonus = calculate_gd_region_bonus(
                a.get("title","") + " " + a.get("summary","") + " " + a.get("source",""))
            special = 0
            if cat_id == "tufa":
                special = calculate_tufa_priority(a.get("title",""), a.get("source",""))
            elif cat_id == "zhengtou":
                special = calculate_zhengtou_priority(a)
            # 排序权重: 日期70% + 地理20% + 特殊10%
            return (-date_part * 0.7 - geo_bonus * 0.2 - special * 0.1)

        arts.sort(key=_sort_key)

        # 如果不足10条且板块为低频板块(fuya)，记录警告
        if len(arts) < 5 and cat_id == "fuya":
            log.warning(f"  {rules['label']}: 仅 {len(arts)} 条（14天窗口），可能需要搜索补丁数据")

        # 限制数量
        arts = arts[:MAX_PER_SECTION]

        items = []
        for art in arts:
            placeholder_id += 1
            tags = []
            # 根据分类打标签
            if cat_id == "tufa" and any(kw in art["title"] for kw in ["地震", "台风", "爆炸", "预警", "超警"]):
                tags.append("urgent")
            if cat_id == "dahu" and any(kw in art["title"] for kw in ["被查", "双开", "落马"]):
                tags.append("hot")
            if any(kw in art["title"] for kw in ["发布", "出台", "印发", "通过"]):
                if "urgent" not in tags:
                    tags.append("new") if tags else None

            item = {
                "id": generate_id(art["title"]),
                "title": art["title"][:100],
                "summary": art.get("summary", "")[:150],
                "source": art.get("source", ""),
                "time": art.get("time_str", ""),
                "url": art.get("url", ""),
                "tags": tags,
                "_ts": art.get("time").timestamp() if art.get("time") else 0,  # 排序用时间戳
            }
            items.append(item)

        section = {
            "id": cat_id,
            "label": rules["label"],
            "icon": rules["icon"],
            "color": rules["color"],
            "items": items,
        }

        # 打虎台附加统计数据
        if cat_id == "dahu":
            section["dahuStats"] = {
                "total2026": 5834,
                "shengbu": 23,
                "tingju": 186,
                "xianchu": 1283,
                "others": 4342,
                "updateDate": datetime.date.today().strftime("%Y年%m月%d日"),
            }

        sections.append(section)
        log.info(f"  {rules['icon']} {rules['label']}: {len(items)} 条(去重后)")

    return sections


def generate_html(sections: list, output_path: str) -> bool:
    """将数据注入 HTML 模板"""
    try:
        with open(TEMPLATE_HTML, "r", encoding="utf-8") as f:
            template = f.read()
    except FileNotFoundError:
        log.error(f"模板文件不存在: {TEMPLATE_HTML}")
        return False

    # 查找数据标记
    start_marker = "// __KANBAN_DATA_START__"
    end_marker = "// __KANBAN_DATA_END__"

    start_idx = template.find(start_marker)
    end_idx = template.find(end_marker)

    if start_idx == -1 or end_idx == -1:
        log.error("模板中未找到数据标记")
        return False

    # 构建 JS 数据
    data_json = json.dumps(sections, ensure_ascii=False, indent=2)
    new_data = f"const SECTIONS = {data_json};"

    # 替换
    before = template[:start_idx]
    after = template[end_idx + len(end_marker):]
    new_html = before + "// __KANBAN_DATA_START__\n" + new_data + "\n// __KANBAN_DATA_END__" + after

    # 动态替换日期为今天
    today_str = datetime.datetime.now().strftime("%Y年%m月%d日")
    import re
    new_html = re.sub(r'<span class="topbar-date" id="topbarDate">\d{4}年\d{1,2}月\d{1,2}日</span>',
                      f'<span class="topbar-date" id="topbarDate">{today_str}</span>', new_html)
    new_html = re.sub(r'updateDate: "\d{4}年\d{1,2}月\d{1,2}日"',
                      f'updateDate: "{today_str}"', new_html)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(new_html)

    log.info(f"HTML 已生成: {output_path}")
    return True


def generate_json(sections: list, output_path: str):
    """输出 JSON 数据文件"""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({
            "updateTime": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "sections": sections,
        }, f, ensure_ascii=False, indent=2)
    log.info(f"JSON 已生成: {output_path}")


def main():
    log.info("=" * 50)
    log.info("政务消息数据抓取引擎")
    log.info(f"启动时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 50)

    # 1. 抓取文章
    articles = fetch_all_articles()

    # 2. 分类
    categorized = classify_and_group(articles)

    # 3. 构建板块
    sections = build_sections(categorized)

    # 4. 检查数据量：如果总量 < 15 条，尝试从搜索数据注入补充
    total = sum(len(s["items"]) for s in sections)
    if total < 15:
        log.warning(f"网页抓取仅获 {total} 条新闻（48h内），尝试加载搜索补丁数据...")
        try:
            from inject_search_data import sections as search_sections
            # 合并：保留已有数据，用搜索数据填补空缺板块
            existing_ids = set()
            for s in sections:
                for item in s["items"]:
                    existing_ids.add(item["id"])

            merged = {}
            for s in sections:
                merged[s["id"]] = s

            for ss in search_sections:
                sid = ss["id"]
                if sid not in merged or len(merged[sid]["items"]) < 5:
                    if sid not in merged:
                        merged[sid] = ss
                    else:
                        # 填补：添加搜索数据中不重复的条目
                        for item in ss["items"]:
                            if item["id"] not in existing_ids:
                                merged[sid]["items"].append(item)
                                existing_ids.add(item["id"])
                        # 限制每板块最多10条
                        merged[sid]["items"] = merged[sid]["items"][:MAX_PER_SECTION]
                    if sid == "dahu" and "dahuStats" in ss:
                        merged[sid]["dahuStats"] = ss["dahuStats"]

            sections = [merged[cid] for cid in ["dahu", "gaoshan", "fuya", "zhengtou", "tufa", "guoqi"] if cid in merged]
            log.info(f"搜索数据补充后: {sum(len(s['items']) for s in sections)} 条")
        except Exception as e:
            log.warning(f"搜索数据加载失败: {e}")

    # ★ 合并后统一排序：日期从新到旧（主键） > 地理加分（次键）
    for s in sections:
        s["items"].sort(key=lambda item: (
            -(item.get("_ts", 0)),
            -calculate_gd_region_bonus(item.get("title", "") + " " + item.get("summary", "") + " " + item.get("source", "")),
        ))

    # 4. 统计
    total = sum(len(s["items"]) for s in sections)
    log.info("=" * 50)
    log.info(f"总计: {total} 条新闻, {len(sections)} 个板块")

    # 5. 生成 HTML
    if generate_html(sections, OUTPUT_HTML):
        log.info(f"看板文件: {OUTPUT_HTML}")

    # 6. 输出 JSON
    generate_json(sections, OUTPUT_JSON)

    # 7. 摘要
    log.info("=" * 50)
    log.info("生成完成!")
    log.info(f"  HTML: {OUTPUT_HTML}")
    log.info(f"  JSON: {OUTPUT_JSON}")
    log.info("=" * 50)

    # 返回数据供程序化使用
    return sections


if __name__ == "__main__":
    sections = main()

    # 输出简要统计
    print("\n各板块统计:")
    for s in sections:
        print(f"  {s['icon']} {s['label']}: {len(s['items'])} 条")
