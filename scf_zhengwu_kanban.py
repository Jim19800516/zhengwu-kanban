#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
政务看板 - 腾讯云 SCF + GitHub Pages 版 (增强版)
=============================================
功能：
1. 定时触发（每日 08:30 / 17:00）自动抓取政务新闻
2. 从多个RSS源、门户网站、搜索引擎聚合抓取数据
3. 按六大板块分类、去重、评分后生成看板 HTML
4. 通过 GitHub API 更新仓库中的 index.html
5. 通过 GitHub Pages URL 直接访问

环境变量（在 SCF 控制台配置）：
- GITHUB_TOKEN: GitHub Personal Access Token（需要 repo 权限）
- GITHUB_OWNER: GitHub 用户名（默认 Jim19800516）
- GITHUB_REPO: 仓库名（默认 zhengwu-kanban）
- GITHUB_BRANCH: 分支名（默认 main）

零第三方依赖，纯 Python 标准库。
"""

import json
import os
import re
import ssl
import time
import base64
import hashlib
import urllib.request
import urllib.error
import urllib.parse
import datetime
from collections import defaultdict
from difflib import SequenceMatcher

# 创建不验证 SSL 证书的上下文
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

# ===== 配置 =====

TEMPLATE_PATH = "zhengwu-kanban.html"
OUTPUT_PATH = "index.html"

# User-Agent 轮换池（模拟不同浏览器和设备，降低被屏蔽概率）
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
]

FETCH_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
}

RETRY_COUNT = 2
RETRY_DELAY = 1
TIMEOUT = 15
MAX_PER_SECTION = 10
SIMILARITY_THRESHOLD = 0.75

BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8))

# 过滤的旧闻关键词
STALE_FILTERS = [
    "新冠", "疫情", "核酸检测", "健康码", "核酸", "无症状感染者",
    "疫苗接种", "抗原", "方舱", "密接", "次密接", "封控", "静默",
    "居家隔离", "复工复产", "动态清零", "乙类乙管",
    "中风险", "高风险", "低风险", "风险区",
    "二十条", "优化防控", "联防联控",
]

# 广东省地理加分
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


# ===== 数据源 =====

RSS_SOURCES = {
    # 官方权威源
    "xinhua_politics": {"url": "http://www.xinhuanet.com/politics/news_politics.xml", "name": "新华网·时政"},
    "xinhua_local": {"url": "http://www.xinhuanet.com/local/news_province.xml", "name": "新华网·地方"},
    "xinhua_legal": {"url": "http://www.xinhuanet.com/legal/news_legal.xml", "name": "新华网·法治"},
    "xinhua_finance": {"url": "http://www.xinhuanet.com/fortune/news_fortune.xml", "name": "新华网·财经"},
    "people_politics": {"url": "http://www.people.com.cn/rss/politics.xml", "name": "人民网·时政"},
    "people_legal": {"url": "http://www.people.com.cn/rss/legal.xml", "name": "人民网·法治"},
    "people_all": {"url": "http://www.people.com.cn/rss/ywkx.xml", "name": "人民网·要闻"},
    # 门户 RSS
    "sina_rss": {"url": "https://rss.sina.com.cn/news/china/focus15.xml", "name": "新浪新闻"},
    "netease_rss": {"url": "http://news.163.com/special/00011K6L/rss_newstop.xml", "name": "网易头条"},
    "netease_guonei": {"url": "http://news.163.com/special/00011K6L/rss_guonei.xml", "name": "网易国内"},
    "sohu_rss": {"url": "http://news.sohu.com/rss/guonei.xml", "name": "搜狐新闻"},
    "ifeng_rss": {"url": "http://news.ifeng.com/rss/index.xml", "name": "凤凰新闻"},
    "ifeng_guonei": {"url": "http://news.ifeng.com/rss/guonei.xml", "name": "凤凰国内"},
    "chinanews_rss": {"url": "https://www.chinanews.com.cn/rss/scroll.xml", "name": "中新网"},
    "chinanews_gn": {"url": "https://www.chinanews.com.cn/rss/gn.xml", "name": "中新网国内"},
    "ce_gdxw": {"url": "http://www.ce.cn/rss/gdxw.xml", "name": "中国经济网"},
    "thepaper_rss": {"url": "https://www.thepaper.cn/rssFeed_china.xml", "name": "澎湃新闻"},
    "huanqiu_rss": {"url": "https://www.huanqiu.com/rss/", "name": "环球网"},
    "guancha_rss": {"url": "https://www.guancha.cn/rss.xml", "name": "观察者网"},
    "cctv_news": {"url": "https://news.cctv.com/china/rss.xml", "name": "央视网"},
    "qq_news": {"url": "https://news.qq.com/newsrss/qq_newschina.xml", "name": "腾讯新闻"},
    "yicai_rss": {"url": "https://www.yicai.com/rss/", "name": "第一财经"},
    "cs_rss": {"url": "http://www.cs.com.cn/ssgs/rss.xml", "name": "中国证券报"},
    # 额外补充源
    "sina_zaobao": {"url": "https://rss.sina.com.cn/news/zaobao/rss.xml", "name": "新浪早报"},
    "sina_gnzx": {"url": "https://rss.sina.com.cn/news/china/gnzx.xml", "name": "新浪国内"},
    "netease_gdxw": {"url": "http://news.163.com/special/00011K6L/rss_gdxw.xml", "name": "网易滚动"},
    "netease_guoji": {"url": "http://news.163.com/special/00011K6L/rss_guoji.xml", "name": "网易国际"},
    "people_society": {"url": "http://www.people.com.cn/rss/society.xml", "name": "人民网·社会"},
    "people_gn": {"url": "http://www.people.com.cn/rss/gn.xml", "name": "人民网·国内"},
}

WEB_SCRAPE_SOURCES = {
    # 门户网站（移动端更友好）
    "sina_news": {"url": "https://news.sina.com.cn/china/", "name": "新浪新闻"},
    "sina_gov": {"url": "https://news.sina.com.cn/gov/", "name": "新浪政务"},
    "netease_news": {"url": "https://news.163.com/domestic/", "name": "网易新闻"},
    "sohu_news": {"url": "https://news.sohu.com/guonei/", "name": "搜狐新闻"},
    "ifeng_news": {"url": "https://news.ifeng.com/c/", "name": "凤凰新闻"},
    "thepaper": {"url": "https://www.thepaper.cn/", "name": "澎湃新闻"},
    "chinanews": {"url": "https://www.chinanews.com.cn/gn/", "name": "中国新闻网"},
    "huanqiu": {"url": "https://www.huanqiu.com/china/", "name": "环球网"},
    "guancha": {"url": "https://www.guancha.cn/mainnews.shtml", "name": "观察者网"},
    "ce_cn": {"url": "http://www.ce.cn/xwzx/gnsz/gdxw/", "name": "中国经济网"},
    # 政府网站（云端可能不可用，但保留）
    "gov_policy": {"url": "https://www.gov.cn/lianbo/", "name": "中国政府网"},
    "sasac_yangqi": {"url": "https://www.sasac.gov.cn/n2588035/n2588320/index.html", "name": "国资委"},
    "mem_accident": {"url": "https://www.mem.gov.cn/gk/sgcc/", "name": "应急管理部"},
    "ccdi_scdc": {"url": "https://www.ccdi.gov.cn/scs/nwt/", "name": "中央纪委"},
    "gd_gzw": {"url": "http://gzw.gd.gov.cn/", "name": "广东省国资委"},
    "sz_gzw": {"url": "https://gzw.sz.gov.cn/gkmlpt/", "name": "深圳市国资委"},
    "gd_gov": {"url": "https://www.gd.gov.cn/gdywdt/zwdt/", "name": "广东省政府"},
    "sz_gov": {"url": "https://www.sz.gov.cn/zwdt/", "name": "深圳市政府"},
    # 招聘相关
    "sasac_zhaopin": {"url": "https://www.sasac.gov.cn/n2588035/n2588320/index.html", "name": "国资委招聘"},
    "gd_gzw_zhaopin": {"url": "http://gzw.gd.gov.cn/zwgk/rsxx/", "name": "广东国资人事"},
}


# ===== 分类规则 =====

CATEGORY_RULES = {
    "zhengtou": {
        "label": "政投先机", "icon": "\U0001f3d7", "color": "#1565c0", "priority": 9,
        "keywords": [
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
            "高铁", "铁路", "机场", "港口", "公路", "桥梁", "隧道",
            "新能源", "半导体", "芯片", "集成电路", "人工智能",
            "数据中心", "算力", "5G", "新基建",
            "央企签约", "央企投资", "国资投资",
        ],
        "exclude": [
            "腐败", "贪腐", "落马", "双规", "双开", "被查", "受贿", "贪污",
            "严重违纪", "审查调查", "开除党籍", "纪律审查",
            "索贿", "贪官", "行贿",
            "731部队", "日军", "罪证", "陈列馆", "公祭日",
            "细菌战", "侵华", "爱国教育", "纪念馆", "遗址", "文物保护",
            "精神落地", "认真学习宣传", "主题教育",
            "宣讲团", "中心组学习", "党委理论学习",
            "学习贯彻", "深入贯彻落实",
            "专题研讨", "培训", "轮训",
            "党史", "党建", "组织生活",
            "最高法", "入额", "挂名办案",
            "传销", "非法集资",
            "炒房", "炒地", "囤地",
            "谣言", "辟谣",
        ],
    },
    "tufa": {
        "label": "突发事件", "icon": "\u26a0", "color": "#e65100", "priority": 9,
        "keywords": [
            "地震", "台风", "暴雨", "洪水", "洪涝", "山体滑坡", "泥石流",
            "冰雹", "暴雪", "寒潮", "高温", "干旱", "森林火灾",
            "爆炸", "火灾", "透水", "坍塌", "坠机", "沉船",
            "公共卫生", "疫情", "猴痘", "传染病", "食品安全",
            "遇难", "伤亡", "失踪", "被困",
            "预警", "应急响应", "救援", "疏散", "转移",
            "气象", "防汛", "抗旱", "安全生产",
        ],
        "exclude": [
            "落马", "双规", "双开", "受贿", "贪污", "腐败", "贪腐",
            "严重违纪", "审查调查", "开除党籍", "纪律审查",
            "精神", "学习", "贯彻", "座谈", "表彰",
            "文艺", "比赛", "演出", "展览",
            "社会救援服务", "服务指南",
        ],
    },
    "guoqi": {
        "label": "国企新闻", "icon": "\U0001f3ed", "color": "#283593", "priority": 9,
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
            "集团", "企业集团", "国企集团",
            "国资监管", "央企负责人", "国企负责人",
            "央企利润", "国企利润", "国资改革",
        ],
        "exclude": ["腐败", "贪腐", "落马", "双规", "双开", "被查", "受贿", "贪污",
            "严重违纪", "审查调查", "开除党籍", "纪律审查"],
    },
    "gaoshan": {
        "label": "高山流水", "icon": "\U0001f3d4", "color": "#2e7d32", "priority": 10,
        "keywords": [
            "人事任免", "干部任免", "人事调整", "人事变动",
            "中央批准", "中央决定",
            "省委书记", "省长", "副省长", "自治区主席",
            "上将军衔", "晋升上将军衔",
            "国务院任免", "全国人大任免",
            "央企主要负责人", "国企负责人",
            "领导职务任免",
            "同志任", "同志不再担任", "同志辞去",
            "国家工作人员任免",
            "任前公示", "拟任", "拟提拔", "拟任命",
            "提名", "当选", "补选", "接替",
            "履新", "晋升", "授衔", "调任", "任职", "免去",
            "换届", "出任", "兼任", "调离",
        ],
        "conditional_keywords": [
            "任命", "任免", "调任", "任职", "免去", "辞去",
            "履新", "晋升", "授衔",
            "当选", "补选", "接替",
            "换届", "出任", "兼任", "调离",
        ],
        "context_keywords": [
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
            "学习", "培训", "表彰", "评选", "获奖",
            "哀悼", "讣告", "逝世", "悼念",
            "召开会议", "座谈会", "主持会议", "讲话",
            "英联邦", "英国", "美国大选", "日本", "韩国总统",
            "奥运会", "世界杯",
        ],
    },
    "dahu": {
        "label": "打虎台", "icon": "\U0001f42f", "color": "#b71c1c", "priority": 10,
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
            "涉嫌严重违纪违法", "接受纪律审查", "接受监察调查",
            "被查", "被逮捕", "被公诉", "被判", "获刑", "移送司法",
        ],
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
        "exclude": [
            "著作权", "商标", "专利", "侵权赔偿",
            "校园欺凌", "校园暴力", "校园",
            "BURBERRY", "尚品网", "假货", "假",
            "未成年人", "未成年",
        ],
    },
    "fuya": {
        "label": "府衙招聘", "icon": "\U0001f4cb", "color": "#6a1b9a", "priority": 10,
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
            "国资委", "政府",
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


# ===== 工具函数 =====

def get_headers():
    """返回随机User-Agent的请求头"""
    import random
    headers = dict(FETCH_HEADERS)
    headers["User-Agent"] = random.choice(USER_AGENTS)
    return headers


def calculate_gd_region_bonus(text):
    max_bonus = 0
    for kw, pts in GD_REGION_KEYWORDS:
        if kw in text:
            if pts > max_bonus:
                max_bonus = pts
    return max_bonus


def is_stale_content(text):
    for kw in STALE_FILTERS:
        if kw in text:
            return True
    return False


def strip_tags(text):
    return re.sub(r'<[^>]+>', '', text).strip()


def fetch_url(url, timeout=TIMEOUT, retries=RETRY_COUNT):
    """用 urllib 抓取 URL，支持随机UA和重试"""
    last_error = None
    for attempt in range(retries + 1):
        try:
            cookie_handler = urllib.request.HTTPCookieProcessor()
            opener = urllib.request.build_opener(
                cookie_handler,
                urllib.request.HTTPSHandler(context=_SSL_CTX),
            )
            headers = get_headers()
            headers["Referer"] = "https://www.baidu.com/"
            req = urllib.request.Request(url, headers=headers)
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                content_type = resp.headers.get('Content-Type', '')
                charset = None
                if 'charset=' in content_type:
                    charset = content_type.split('charset=')[-1].strip().strip('"')
                if not charset:
                    head = raw[:2048].decode('ascii', errors='ignore')
                    m = re.search(r'charset=["\']?([\w-]+)', head, re.IGNORECASE)
                    if m:
                        charset = m.group(1)
                if not charset:
                    charset = 'utf-8'
                try:
                    return raw.decode(charset)
                except (UnicodeDecodeError, LookupError):
                    try:
                        return raw.decode('gb2312')
                    except:
                        return raw.decode('utf-8', errors='replace')
        except Exception as e:
            last_error = e
            if attempt < retries:
                time.sleep(RETRY_DELAY)
            continue
    print(f"  [FAIL] 抓取失败: {url} -- {last_error}")
    return None


def parse_rss(url, source_name):
    """用正则解析 RSS feed"""
    xml = fetch_url(url)
    if not xml:
        return []

    articles = []
    items = re.findall(r'<item[^>]*>(.*?)</item>', xml, re.DOTALL | re.IGNORECASE)
    if not items:
        items = re.findall(r'<entry[^>]*>(.*?)</entry>', xml, re.DOTALL | re.IGNORECASE)

    for item in items[:60]:
        title_m = re.search(r'<title[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>', item, re.DOTALL)
        link_m = re.search(r'<link[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>', item, re.DOTALL)
        if not link_m:
            link_m = re.search(r'<link[^>]*href=["\']([^"\']+)["\']', item)
        desc_m = re.search(r'<description[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>', item, re.DOTALL)
        date_m = re.search(r'<pubDate[^>]*>(.*?)</pubDate>', item, re.DOTALL | re.IGNORECASE)
        if not date_m:
            date_m = re.search(r'<updated[^>]*>(.*?)</updated>', item, re.DOTALL | re.IGNORECASE)
        if not date_m:
            date_m = re.search(r'<published[^>]*>(.*?)</published>', item, re.DOTALL | re.IGNORECASE)
        if not date_m:
            date_m = re.search(r'<dc:date[^>]*>(.*?)</dc:date>', item, re.DOTALL | re.IGNORECASE)

        title = strip_tags(title_m.group(1).strip()) if title_m else ""
        link = ""
        if link_m:
            link = link_m.group(1).strip()
        summary = strip_tags(desc_m.group(1).strip())[:200] if desc_m else ""
        published = date_m.group(1).strip() if date_m else ""

        if not title or not link:
            continue
        if is_stale_content(f"{title} {summary}"):
            continue

        dt = _parse_datetime(time_str=published, url=link)
        if not dt:
            dt = datetime.datetime.now()

        if not _is_recent(dt, max_age_hours=336):  # 14天内
            continue

        articles.append({
            "title": title[:100],
            "url": link,
            "summary": summary,
            "time": dt,
            "time_str": _format_time(dt),
            "source": source_name,
        })

    return articles


def scrape_webpage(url, source_name):
    """用正则解析网页，提取新闻链接和标题"""
    html = fetch_url(url)
    if not html:
        return []

    articles = []
    seen_urls = set()
    now = datetime.datetime.now()

    pattern = r'<a[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>'
    for m in re.finditer(pattern, html, re.DOTALL | re.IGNORECASE):
        href = m.group(1)
        inner = m.group(2)
        title = strip_tags(inner).strip()

        if not title and 'title=' in m.group(0):
            tm = re.search(r'title=["\']([^"\']+)["\']', m.group(0))
            if tm:
                title = tm.group(1).strip()

        if not title or len(title) < 8:
            continue

        if not href or href.startswith("javascript:") or href == "#" or href.startswith("mailto:"):
            continue
        if any(kw in href for kw in [".jpg", ".png", ".gif", ".mp4", ".pdf", "/login", "/register"]):
            continue
        if any(kw in title for kw in ["更多", "下一页", "上一页", "登录", "注册", "专题", "组图", "视频"]):
            continue

        if not href.startswith("http"):
            domain = "/".join(url.split("/")[:3])
            if href.startswith("/"):
                href = domain + href
            elif href.startswith("./"):
                href = url.rsplit("/", 1)[0] + href[1:]
            else:
                href = url.rsplit("/", 1)[0] + "/" + href

        if href in seen_urls:
            continue
        seen_urls.add(href)

        if is_stale_content(title):
            continue

        dt = _parse_datetime(url=href)
        start = max(0, m.start() - 300)
        end = min(len(html), m.end() + 300)
        context = html[start:end]

        if not dt:
            dt = _parse_datetime_from_context(context)

        summary = ""
        text_ctx = strip_tags(context).replace(title, "").strip()
        text_ctx = re.sub(r'\s+', ' ', text_ctx)
        if len(text_ctx) > 20:
            summary = text_ctx[:200]

        if not dt:
            dt = now

        articles.append({
            "title": title[:100],
            "url": href,
            "summary": summary,
            "time": dt,
            "time_str": _format_time(dt) if dt else "",
            "source": source_name,
        })

        if len(articles) >= 80:
            break

    return articles


# ===== 搜索引擎聚合抓取 =====

def scrape_baidu_news(query, source_name="百度新闻"):
    """从百度新闻搜索抓取结果"""
    try:
        encoded = urllib.parse.quote(query)
    except Exception:
        encoded = urllib.parse.quote(query.encode('utf-8'))
    search_url = f"https://news.baidu.com/ns?word={encoded}&tn=newstitle&from=news&cl=2&rn=50&ct=0"

    html = fetch_url(search_url, timeout=15)
    if not html:
        return []

    articles = []
    seen_urls = set()
    now = datetime.datetime.now()

    # 百度新闻结果：多种可能的结构
    patterns = [
        r'<h3[^>]*class=["\']c-title["\'][^>]*>\s*<a[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        r'<a[^>]*href=["\']([^"\']+)["\'][^>]*class=["\'][^"\']*title[^"\']*["\'][^>]*>(.*?)</a>',
        r'<a[^>]*href=["\']([^"\']+)["\'][^>]*>\s*(.*?)\s*</a>\s*<span[^>]*class=["\']news-source["\']',
    ]

    for pattern in patterns:
        for m in re.finditer(pattern, html, re.DOTALL | re.IGNORECASE):
            href = m.group(1)
            title = strip_tags(m.group(2)).strip()

            if not title or len(title) < 8 or len(title) > 100:
                continue
            if href in seen_urls:
                continue
            seen_urls.add(href)

            # 百度跳转链接解码
            if href.startswith("http://news.baidu.com/ns?") or href.startswith("https://news.baidu.com/ns?"):
                real_m = re.search(r'url=([^&]+)', href)
                if real_m:
                    try:
                        href = urllib.parse.unquote(real_m.group(1))
                    except Exception:
                        pass

            if is_stale_content(title):
                continue
            if any(kw in title for kw in ["百度", "推广", "广告", "登录", "注册"]):
                continue

            dt = _parse_datetime(url=href) or now

            articles.append({
                "title": title[:100],
                "url": href,
                "summary": "",
                "time": dt,
                "time_str": _format_time(dt),
                "source": source_name,
            })

            if len(articles) >= 30:
                break
        if len(articles) >= 30:
            break

    return articles


def scrape_sogou_weixin(query, source_name="搜狗微信"):
    """从搜狗微信搜索抓取公众号文章（政务新闻覆盖很好）"""
    try:
        encoded = urllib.parse.quote(query)
    except Exception:
        encoded = urllib.parse.quote(query.encode('utf-8'))
    search_url = f"https://weixin.sogou.com/weixin?type=2&query={encoded}&ie=utf8"

    headers = get_headers()
    headers["Referer"] = "https://weixin.sogou.com/"

    html = fetch_url(search_url, timeout=15)
    if not html:
        return []

    articles = []
    seen_urls = set()
    now = datetime.datetime.now()

    # 搜狗微信结果结构
    pattern = r'<li[^>]*id=["\']sogou_vr_[\d_]+["\'][^>]*>.*?<h3>.*?<a[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>.*?</h3>.*?</li>'
    for m in re.finditer(pattern, html, re.DOTALL | re.IGNORECASE):
        href = m.group(1)
        title = strip_tags(m.group(2)).strip()

        if not title or len(title) < 8:
            continue
        if href in seen_urls:
            continue
        seen_urls.add(href)

        # 搜狗微信链接需要补全
        if not href.startswith("http"):
            href = "https://weixin.sogou.com" + href

        if is_stale_content(title):
            continue

        dt = _parse_datetime(url=href) or now

        articles.append({
            "title": title[:100],
            "url": href,
            "summary": "",
            "time": dt,
            "time_str": _format_time(dt),
            "source": source_name,
        })

        if len(articles) >= 20:
            break

    # 备用pattern
    if not articles:
        pattern2 = r'<a[^>]*target=["\']_blank["\'][^>]*href=["\']([^"\']+)["\'][^>]*>\s*(.*?)\s*</a>'
        for m in re.finditer(pattern2, html, re.DOTALL | re.IGNORECASE):
            href = m.group(1)
            title = strip_tags(m.group(2)).strip()
            if not title or len(title) < 8 or len(title) > 100:
                continue
            if href in seen_urls:
                continue
            seen_urls.add(href)
            if not href.startswith("http"):
                href = "https://weixin.sogou.com" + href
            if is_stale_content(title):
                continue
            dt = _parse_datetime(url=href) or now
            articles.append({
                "title": title[:100],
                "url": href,
                "summary": "",
                "time": dt,
                "time_str": _format_time(dt),
                "source": source_name,
            })
            if len(articles) >= 20:
                break

    return articles


def scrape_toutiao_search(query, source_name="头条搜索"):
    """从头条搜索抓取新闻"""
    try:
        encoded = urllib.parse.quote(query)
    except Exception:
        encoded = urllib.parse.quote(query.encode('utf-8'))
    search_url = f"https://so.toutiao.com/search?keyword={encoded}&pd=news&source=search_subtab_switch"

    html = fetch_url(search_url, timeout=15)
    if not html:
        return []

    articles = []
    seen_urls = set()
    now = datetime.datetime.now()

    # 尝试从JSON数据中提取
    json_matches = re.findall(r'"title":"([^"]+)".*?"url":"([^"]+)"', html)
    for title, url in json_matches:
        title = title.encode('utf-8').decode('unicode_escape') if '\\u' in title else title
        title = strip_tags(title).strip()
        if not title or len(title) < 8:
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        if is_stale_content(title):
            continue
        dt = _parse_datetime(url=url) or now
        articles.append({
            "title": title[:100],
            "url": url,
            "summary": "",
            "time": dt,
            "time_str": _format_time(dt),
            "source": source_name,
        })
        if len(articles) >= 20:
            break

    return articles


def scrape_bing_news(query, source_name="必应新闻"):
    """从必应新闻搜索抓取"""
    try:
        encoded = urllib.parse.quote(query)
    except Exception:
        encoded = urllib.parse.quote(query.encode('utf-8'))
    search_url = f"https://cn.bing.com/news/search?q={encoded}&FORM=HDRSC7"

    html = fetch_url(search_url, timeout=15)
    if not html:
        return []

    articles = []
    seen_urls = set()
    now = datetime.datetime.now()

    pattern = r'<a[^>]*class=["\']title["\'][^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>'
    for m in re.finditer(pattern, html, re.DOTALL | re.IGNORECASE):
        href = m.group(1)
        title = strip_tags(m.group(2)).strip()

        if not title or len(title) < 8:
            continue
        if href in seen_urls:
            continue
        seen_urls.add(href)

        if is_stale_content(title):
            continue

        dt = _parse_datetime(url=href) or now

        articles.append({
            "title": title[:100],
            "url": href,
            "summary": "",
            "time": dt,
            "time_str": _format_time(dt),
            "source": source_name,
        })

        if len(articles) >= 20:
            break

    return articles


def scrape_360_news(query, source_name="360新闻"):
    """从360新闻搜索抓取"""
    try:
        encoded = urllib.parse.quote(query)
    except Exception:
        encoded = urllib.parse.quote(query.encode('utf-8'))
    search_url = f"https://news.so.com/ns?q={encoded}&tn=news&rank=pdate&src=srp"

    html = fetch_url(search_url, timeout=15)
    if not html:
        return []

    articles = []
    seen_urls = set()
    now = datetime.datetime.now()

    pattern = r'<h3[^>]*>\s*<a[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>\s*</h3>'
    for m in re.finditer(pattern, html, re.DOTALL | re.IGNORECASE):
        href = m.group(1)
        title = strip_tags(m.group(2)).strip()

        if not title or len(title) < 8:
            continue
        if href in seen_urls:
            continue
        seen_urls.add(href)

        if is_stale_content(title):
            continue

        dt = _parse_datetime(url=href) or now

        articles.append({
            "title": title[:100],
            "url": href,
            "summary": "",
            "time": dt,
            "time_str": _format_time(dt),
            "source": source_name,
        })

        if len(articles) >= 20:
            break

    return articles


# ===== 按板块搜索 =====

def search_by_category():
    """
    按板块使用多个搜索引擎抓取，每个板块用多个关键词组合搜索。
    这是核心兜底策略，确保每个板块都有数据。
    """
    search_configs = {
        "dahu": [
            ("落马 2026", "百度"),
            ("双开 2026", "搜狗微信"),
            ("审查调查 2026", "360新闻"),
            ("中央纪委 2026", "必应"),
        ],
        "gaoshan": [
            ("任命 2026", "百度"),
            ("干部任免 2026", "搜狗微信"),
            ("履新 2026", "360新闻"),
            ("任前公示 2026", "必应"),
        ],
        "zhengtou": [
            ("重大项目 签约 2026", "百度"),
            ("发改委 批复 2026", "搜狗微信"),
            ("投资 开工 2026", "360新闻"),
            ("专项债 2026", "必应"),
        ],
        "tufa": [
            ("应急管理部 事故 2026", "百度"),
            ("台风 2026", "搜狗微信"),
            ("地震 2026", "360新闻"),
            ("安全生产 2026", "必应"),
        ],
        "guoqi": [
            ("国资委 央企 2026", "百度"),
            ("国企改革 2026", "搜狗微信"),
            ("国有企业 2026", "360新闻"),
            ("央企利润 2026", "必应"),
        ],
        "fuya": [
            ("国企招聘 2026", "百度"),
            ("央企招聘 2026", "搜狗微信"),
            ("公开招聘 2026", "360新闻"),
            ("事业单位招聘 2026", "必应"),
        ],
    }

    all_articles = []
    search_functions = {
        "百度": scrape_baidu_news,
        "搜狗微信": scrape_sogou_weixin,
        "360新闻": scrape_360_news,
        "必应": scrape_bing_news,
        "头条": scrape_toutiao_search,
    }

    for cat_id, queries in search_configs.items():
        for query, engine in queries:
            fn = search_functions.get(engine, scrape_baidu_news)
            try:
                arts = fn(query, source_name=f"{engine}·{cat_id}")
                if arts:
                    print(f"  [OK] {engine} 搜索 [{cat_id}]: {query} -> {len(arts)} 条")
                    all_articles.extend(arts)
                else:
                    print(f"  [FAIL] {engine} 搜索 [{cat_id}]: {query} -> 0 条")
            except Exception as e:
                print(f"  [ERR] {engine} 搜索 [{cat_id}]: {query} -> {e}")
            time.sleep(0.3)

    return all_articles


# ===== 日期解析 =====

def _parse_datetime_from_context(context):
    date_patterns = [
        r'(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})',
        r'(\d{4})-(\d{2})-(\d{2})',
        r'(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{2}):(\d{2})',
        r'(\d{4})年(\d{1,2})月(\d{1,2})日',
        r'\[(\d{4})-(\d{2})-(\d{2})\]',
        r'(\d{4})/(\d{1,2})/(\d{1,2})',
    ]
    for dp in date_patterns:
        dm = re.search(dp, context)
        if dm:
            try:
                groups = dm.groups()
                year = int(groups[0])
                month = int(groups[1])
                day = int(groups[2])
                if 2020 <= year <= 2030 and 1 <= month <= 12 and 1 <= day <= 31:
                    return datetime.datetime(year, month, day)
            except (ValueError, IndexError):
                pass
    return None


def _parse_datetime(time_str="", url=None):
    if url:
        m = re.search(r'/(\d{4})-(\d{2})/?(\d{2})/', url)
        if not m:
            m = re.search(r'/(\d{4})/(\d{2})/(\d{2})/', url)
        if not m:
            m = re.search(r'/(\d{2})/(\d{2})(\d{2})/\d{2}/', url)
            if m:
                year = 2000 + int(m.group(1))
                return datetime.datetime(year, int(m.group(2)), int(m.group(3)))
        if not m:
            m = re.search(r'/(\d{4})(\d{2})(\d{2})/', url)
        if m:
            try:
                return datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass

    if time_str:
        time_str = time_str.strip()
        m = re.search(r'\[(\d{4})-(\d{2})-(\d{2})\]', time_str)
        if m:
            try:
                return datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
        for fmt in [
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
            "%a, %d %b %Y %H:%M:%S", "%Y年%m月%d日 %H:%M:%S", "%Y年%m月%d日",
        ]:
            try:
                return datetime.datetime.strptime(
                    time_str[:19] if len(time_str) > 10 else time_str, fmt)
            except ValueError:
                continue

    return None


def _format_time(dt=None):
    if dt is None:
        return ""
    return f"{dt.month}月{dt.day}日"


def _is_recent(dt, max_age_hours=336):
    if dt is None:
        return False
    now = datetime.datetime.now()
    delta = now - dt
    return delta.total_seconds() <= max_age_hours * 3600


# ===== 分类与排序 =====

def classify_article_with_scores(article):
    title = article.get("title", "")
    summary = article.get("summary", "")
    text = f"{title} {summary}"

    gd_bonus = calculate_gd_region_bonus(text)
    scores = defaultdict(int)

    for cat_id, rules in CATEGORY_RULES.items():
        excluded = False
        for kw in rules.get("exclude", []):
            if kw in text:
                excluded = True
                break
        if excluded:
            continue

        for kw in rules.get("keywords", []):
            if kw in text:
                scores[cat_id] += 1

        cond_kws = rules.get("conditional_keywords", [])
        ctx_kws = rules.get("context_keywords", [])
        if cond_kws and ctx_kws:
            has_context = any(kw in text for kw in ctx_kws)
            if has_context:
                for kw in cond_kws:
                    if kw in text:
                        scores[cat_id] += 1

    results = []
    for cat_id, score in scores.items():
        if score > 0:
            results.append((cat_id, score + CATEGORY_RULES[cat_id]["priority"] + gd_bonus))

    return results


def title_similarity(t1, t2):
    def clean(s):
        return re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", "", s).lower()
    c1, c2 = clean(t1), clean(t2)
    if not c1 or not c2:
        return 0
    return SequenceMatcher(None, c1, c2).ratio()


def deduplicate(articles, threshold=SIMILARITY_THRESHOLD):
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


def calculate_zhengtou_priority(article):
    title = article.get("title", "")
    summary = article.get("summary", "")
    text = f"{title} {summary}"
    score = 0

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
            break

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

    score += calculate_gd_region_bonus(text)
    return score


def calculate_tufa_priority(title, source):
    score = 0
    text = title + " " + source

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

    regional_kw = [
        "台风", "长江", "黄河", "珠江", "淮河", "海河", "松花江",
        "流域", "干流", "全线", "超警",
    ]
    for kw in regional_kw:
        if kw in text:
            score += 25
            break

    province_kw = ["省", "自治区"]
    for kw in province_kw:
        if kw in text and "全国" not in text:
            score += 15
            break

    city_kw = ["市", "县", "区"]
    for kw in city_kw:
        if kw in text and score < 15:
            score += 5
            break

    urgency_bonus = 0
    if any(kw in text for kw in ["预警", "红色预警", "橙色预警", "超警"]):
        urgency_bonus += 5
    if any(kw in text for kw in ["遇难", "死亡", "伤亡", "失踪"]):
        urgency_bonus += 5

    gd_bonus = calculate_gd_region_bonus(text)
    return score + urgency_bonus + gd_bonus


def generate_id(text):
    return int(hashlib.md5(text.encode()).hexdigest()[:8], 16) % 9000 + 1000


# ===== 数据抓取 =====

def fetch_all_articles():
    """从所有源抓取文章"""
    all_articles = []
    rss_success = 0
    web_success = 0

    print("=" * 50)
    print("第一阶段: RSS 数据源抓取...")
    for source_id, source_info in RSS_SOURCES.items():
        articles = parse_rss(source_info["url"], source_info["name"])
        all_articles.extend(articles)
        if articles:
            print(f"  [OK] {source_info['name']}: {len(articles)} 条")
            rss_success += 1
        else:
            print(f"  [FAIL] {source_info['name']}: 0 条")

    print("第二阶段: 网页直接抓取...")
    for source_id, source_info in WEB_SCRAPE_SOURCES.items():
        articles = scrape_webpage(source_info["url"], source_info["name"])
        all_articles.extend(articles)
        if articles:
            print(f"  [OK] {source_info['name']}: {len(articles)} 条")
            web_success += 1
        else:
            print(f"  [FAIL] {source_info['name']}: 0 条")

    print("第三阶段: 搜索引擎聚合抓取...")
    search_articles = search_by_category()
    if search_articles:
        print(f"  [OK] 搜索引擎聚合: {len(search_articles)} 条")
        all_articles.extend(search_articles)
    else:
        print(f"  [FAIL] 搜索引擎聚合: 0 条")

    # 全局日期过滤：14天
    before = len(all_articles)
    all_articles = [a for a in all_articles if a.get("time") and _is_recent(a["time"], max_age_hours=336)]
    dropped = before - len(all_articles)
    if dropped:
        print(f"全局日期过滤: 丢弃 {dropped} 条过期/无日期新闻, 保留 {len(all_articles)} 条")

    print(f"RSS: {rss_success}/{len(RSS_SOURCES)} | 网页: {web_success}/{len(WEB_SCRAPE_SOURCES)} | 总计: {len(all_articles)} 条")
    return all_articles


def classify_and_group(articles):
    """分类并分组，跨板块去重"""
    articles = deduplicate(articles)

    article_candidates = []
    for art in articles:
        cats_with_scores = classify_article_with_scores(art)
        if cats_with_scores:
            article_candidates.append((art, cats_with_scores))

    categorized = defaultdict(list)
    for art, candidates in article_candidates:
        candidates.sort(key=lambda x: -x[1])
        best_cat = candidates[0][0]
        categorized[best_cat].append(art)

    print("分类情况（跨板块去重后）:")
    for cat_id, rules in CATEGORY_RULES.items():
        count = len(categorized.get(cat_id, []))
        print(f"  {rules['icon']} {rules['label']}: {count} 条")

    return categorized


def build_sections(categorized):
    """构建最终板块数据"""
    sections = []

    SECTION_TIME_WINDOWS = {
        "dahu": 168, "gaoshan": 168, "fuya": 336,
        "zhengtou": 168, "tufa": 72, "guoqi": 168,
    }

    for cat_id in ["dahu", "gaoshan", "fuya", "zhengtou", "tufa", "guoqi"]:
        rules = CATEGORY_RULES[cat_id]
        arts = categorized.get(cat_id, [])

        arts = deduplicate(arts)

        max_hours = SECTION_TIME_WINDOWS.get(cat_id, 168)
        arts = [a for a in arts if a.get("time") and _is_recent(a["time"], max_age_hours=max_hours)]

        def _sort_key(a):
            dt = a.get("time")
            date_part = dt.timestamp() if dt else 0
            geo_bonus = calculate_gd_region_bonus(
                a.get("title", "") + " " + a.get("summary", "") + " " + a.get("source", ""))
            special = 0
            if cat_id == "tufa":
                special = calculate_tufa_priority(a.get("title", ""), a.get("source", ""))
            elif cat_id == "zhengtou":
                special = calculate_zhengtou_priority(a)
            return (-date_part * 0.7 - geo_bonus * 0.2 - special * 0.1)

        arts.sort(key=_sort_key)
        arts = arts[:MAX_PER_SECTION]

        items = []
        for art in arts:
            tags = []
            if cat_id == "tufa" and any(kw in art["title"] for kw in ["地震", "台风", "爆炸", "预警", "超警"]):
                tags.append("urgent")
            if cat_id == "dahu" and any(kw in art["title"] for kw in ["被查", "双开", "落马"]):
                tags.append("hot")

            item = {
                "id": generate_id(art["title"]),
                "title": art["title"][:100],
                "summary": art.get("summary", "")[:150],
                "source": art.get("source", ""),
                "time": art.get("time_str", ""),
                "url": art.get("url", ""),
                "tags": tags,
                "_ts": art.get("time").timestamp() if art.get("time") else 0,
            }
            items.append(item)

        # 如果板块没有数据，添加提示信息
        if not items:
            items.append({
                "id": generate_id(f"no_data_{cat_id}"),
                "title": "暂无可展示的新闻数据",
                "summary": "当前抓取渠道可能暂时受限，请稍后刷新或检查数据源。",
                "source": "系统提示",
                "time": "",
                "url": "",
                "tags": [],
                "_ts": 0,
            })

        section = {
            "id": cat_id,
            "label": rules["label"],
            "icon": rules["icon"],
            "color": rules["color"],
            "items": items,
        }

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
        print(f"  {rules['icon']} {rules['label']}: {len(items)} 条")

    return sections


# ===== HTML 生成 =====

def generate_html(sections, template_html):
    """将数据注入 HTML 模板"""
    start_marker = "// __KANBAN_DATA_START__"
    end_marker = "// __KANBAN_DATA_END__"

    start_idx = template_html.find(start_marker)
    end_idx = template_html.find(end_marker)

    if start_idx == -1 or end_idx == -1:
        raise RuntimeError("模板中未找到数据标记 __KANBAN_DATA_START__/END__")

    data_json = json.dumps(sections, ensure_ascii=False, indent=2)
    new_data = f"const SECTIONS = {data_json};"

    before = template_html[:start_idx]
    after = template_html[end_idx + len(end_marker):]
    new_html = before + "// __KANBAN_DATA_START__\n" + new_data + "\n// __KANBAN_DATA_END__" + after

    now_bj = datetime.datetime.now(BEIJING_TZ)
    today_str = f"{now_bj.year}年{now_bj.month}月{now_bj.day}日"
    new_html = re.sub(
        r'<span class="topbar-date" id="topbarDate">\d{4}年\d{1,2}月\d{1,2}日</span>',
        f'<span class="topbar-date" id="topbarDate">{today_str}</span>',
        new_html)
    new_html = re.sub(
        r'updateDate: "\d{4}年\d{1,2}月\d{1,2}日"',
        f'updateDate: "{today_str}"',
        new_html)

    return new_html


# ===== GitHub API =====

def github_get_file(owner, repo, path, branch, token):
    """获取 GitHub 仓库中文件的内容和 SHA"""
    api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={branch}"
    req = urllib.request.Request(api_url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "SCF-Zhengwu-Kanban")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            sha = data.get("sha", "")
            content_b64 = data.get("content", "")
            content_b64_clean = content_b64.replace("\n", "")
            content = base64.b64decode(content_b64_clean).decode("utf-8")
            return content, sha
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"  文件不存在: {path}")
            return None, None
        error_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API HTTP {e.code}: {error_body}")


def github_upload_file(html_content, owner, repo, path, branch, token, sha=None):
    """通过 GitHub Contents API 上传文件"""
    content_b64 = base64.b64encode(html_content.encode("utf-8")).decode("ascii")
    now_bj = datetime.datetime.now(BEIJING_TZ)
    commit_msg = f"政务看板自动更新 - {now_bj.strftime('%Y-%m-%d %H:%M')} 北京时间（SCF云函数）"

    payload = {
        "message": commit_msg,
        "content": content_b64,
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    payload_json = json.dumps(payload).encode("utf-8")
    api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    req = urllib.request.Request(api_url, data=payload_json, method="PUT")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("User-Agent", "SCF-Zhengwu-Kanban")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp_data = json.loads(resp.read().decode("utf-8"))
            commit_sha = resp_data.get("commit", {}).get("sha", "")
            print(f"  GitHub 上传成功: {owner}/{repo}/{path} @ {branch}, commit: {commit_sha[:8]}")
            return commit_sha
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub 上传失败 HTTP {e.code}: {error_body}")


# ===== SCF 入口 =====

def main_handler(event, context):
    """腾讯云 SCF 入口函数"""
    print("=" * 50)
    print("政务看板 SCF 云函数启动 (增强版)")
    print(f"时间: {datetime.datetime.now(BEIJING_TZ).strftime('%Y-%m-%d %H:%M:%S')} 北京时间")
    print("=" * 50)

    token = os.environ.get("GITHUB_TOKEN", "")
    owner = os.environ.get("GITHUB_OWNER", "Jim19800516")
    repo = os.environ.get("GITHUB_REPO", "zhengwu-kanban")
    branch = os.environ.get("GITHUB_BRANCH", "main")

    if not token:
        return {
            "isBase64Encoded": False,
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"code": -1, "message": "缺少环境变量 GITHUB_TOKEN"}, ensure_ascii=False),
        }

    try:
        print("步骤1: 获取模板 HTML...")
        template_html, template_sha = github_get_file(owner, repo, TEMPLATE_PATH, branch, token)
        if not template_html:
            return {
                "statusCode": 500,
                "body": json.dumps({"code": -1, "message": f"模板文件 {TEMPLATE_PATH} 不存在于仓库"}, ensure_ascii=False),
            }
        print(f"  模板获取成功: {len(template_html)} 字节")

        print("步骤2: 抓取政务新闻数据...")
        articles = fetch_all_articles()

        print("步骤3: 分类与去重...")
        categorized = classify_and_group(articles)

        print("步骤4: 构建板块数据...")
        sections = build_sections(categorized)

        print("步骤5: 生成看板 HTML...")
        html_content = generate_html(sections, template_html)

        print("步骤6: 获取 index.html 当前 SHA...")
        _, index_sha = github_get_file(owner, repo, OUTPUT_PATH, branch, token)

        print("步骤7: 上传到 GitHub...")
        commit_sha = github_upload_file(
            html_content, owner, repo, OUTPUT_PATH, branch, token, index_sha)

        total = sum(len(s["items"]) for s in sections)
        real_total = sum(len([i for i in s["items"] if i.get("url")]) for s in sections)
        print("=" * 50)
        print(f"完成! 总计 {total} 条展示 ({real_total} 条有效新闻), {len(sections)} 个板块")
        print(f"Commit: {commit_sha[:8]}")
        print(f"GitHub Pages: https://{owner}.github.io/{repo}/")
        print("=" * 50)

        stats = {s["id"]: len([i for i in s["items"] if i.get("url")]) for s in sections}

        return {
            "isBase64Encoded": False,
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "code": 0,
                "message": "政务看板更新成功",
                "total": real_total,
                "commit": commit_sha[:8],
                "sections": stats,
                "updateTime": datetime.datetime.now(BEIJING_TZ).strftime('%Y-%m-%d %H:%M:%S'),
            }, ensure_ascii=False),
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {
            "isBase64Encoded": False,
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"code": -1, "message": f"失败: {str(e)}"}, ensure_ascii=False),
        }


# ===== 本地测试入口 =====

if __name__ == "__main__":
    if not os.environ.get("GITHUB_TOKEN"):
        print("请设置环境变量 GITHUB_TOKEN")
        print("示例: export GITHUB_TOKEN=ghp_xxxxxxxxxxxx")
        exit(1)

    result = main_handler({}, None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
