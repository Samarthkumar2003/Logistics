"""
port_normalizer.py
------------------
Normalizes port/city/country name variations to canonical forms before
vector search and SQL filtering.

Problem: "Nhava Sheva", "JNPT", "Mumbai Port" are the same port.
SQL filter `lower(origin) = lower(:origin)` fails on any variation
→ history search returns zero results → falls back to AI estimate.

Solution: map all known aliases to one canonical name per location.
"""

# ── Alias map: alias (lowercase) → canonical name ────────────────────────────
# Each canonical name is what gets stored in the DB and used in queries.
_ALIASES: dict[str, str] = {

    # ════════════════════════════════════════════════════════════
    # INDIA — Sea Ports
    # ════════════════════════════════════════════════════════════
    "nhava sheva": "Nhava Sheva, India",
    "jnpt": "Nhava Sheva, India",
    "jawaharlal nehru port": "Nhava Sheva, India",
    "jawaharlal nehru port trust": "Nhava Sheva, India",
    "mumbai port": "Nhava Sheva, India",
    "mumbai": "Nhava Sheva, India",
    "bombay": "Nhava Sheva, India",
    "nsict": "Nhava Sheva, India",
    "mundra": "Mundra Port, India",
    "mundra port": "Mundra Port, India",
    "adani mundra": "Mundra Port, India",
    "mun": "Mundra Port, India",
    "cochin": "Cochin, India",
    "kochi": "Cochin, India",
    "cochin port": "Cochin, India",
    "kochi port": "Cochin, India",
    "cok": "Cochin, India",
    "chennai": "Chennai, India",
    "chennai port": "Chennai, India",
    "madras": "Chennai, India",
    "maa": "Chennai, India",
    "kolkata": "Kolkata, India",
    "calcutta": "Kolkata, India",
    "haldia": "Kolkata, India",
    "kolkata port": "Kolkata, India",
    "ccu": "Kolkata, India",
    "kandla": "Kandla, India",
    "deendayal port": "Kandla, India",
    "pipavav": "Pipavav, India",
    "aph": "Pipavav, India",
    "hazira": "Hazira, India",
    "ennore": "Ennore, India",
    "kamarajar port": "Ennore, India",
    "mangalore": "Mangalore, India",
    "new mangalore port": "Mangalore, India",
    "mng": "Mangalore, India",
    "visakhapatnam": "Visakhapatnam, India",
    "vizag": "Visakhapatnam, India",
    "vtz": "Visakhapatnam, India",
    "tuticorin": "Tuticorin, India",
    "thoothukudi": "Tuticorin, India",
    "vot": "Tuticorin, India",
    "tut": "Tuticorin, India",
    "mormugao": "Mormugao, India",
    "goa port": "Mormugao, India",
    "mrm": "Mormugao, India",
    "paradip": "Paradip, India",
    "pdt": "Paradip, India",

    # ════════════════════════════════════════════════════════════
    # INDIA — Air
    # ════════════════════════════════════════════════════════════
    "bangalore": "Bangalore, India",
    "bengaluru": "Bangalore, India",
    "blr": "Bangalore, India",
    "delhi": "Delhi, India",
    "new delhi": "Delhi, India",
    "indira gandhi international": "Delhi, India",
    "del": "Delhi, India",
    "hyderabad": "Hyderabad, India",
    "hyd": "Hyderabad, India",
    "rajiv gandhi international": "Hyderabad, India",
    "pune": "Pune, India",
    "pnq": "Pune, India",
    "ahmedabad": "Ahmedabad, India",
    "amd": "Ahmedabad, India",
    "coimbatore": "Coimbatore, India",
    "cjb": "Coimbatore, India",
    "lucknow": "Lucknow, India",
    "lko": "Lucknow, India",
    "nagpur": "Nagpur, India",
    "nag": "Nagpur, India",
    "kochi airport": "Cochin, India",
    "trivandrum": "Trivandrum, India",
    "thiruvananthapuram": "Trivandrum, India",
    "trv": "Trivandrum, India",
    "surat": "Surat, India",
    "stu": "Surat, India",
    "amritsar": "Amritsar, India",
    "atr": "Amritsar, India",
    "bhubaneswar": "Bhubaneswar, India",
    "bbi": "Bhubaneswar, India",
    "guwahati": "Guwahati, India",
    "gau": "Guwahati, India",
    "jaipur": "Jaipur, India",
    "jai": "Jaipur, India",
    "patna": "Patna, India",
    "pat": "Patna, India",
    "varanasi": "Varanasi, India",
    "vns": "Varanasi, India",
    "indore": "Indore, India",
    "idr": "Indore, India",

    # ════════════════════════════════════════════════════════════
    # UAE — Sea Ports
    # ════════════════════════════════════════════════════════════
    "jebel ali": "Jebel Ali, UAE",
    "jebel ali port": "Jebel Ali, UAE",
    "jabal ali": "Jebel Ali, UAE",
    "jea": "Jebel Ali, UAE",
    "dubai port": "Jebel Ali, UAE",
    "port rashid": "Jebel Ali, UAE",
    "dpworld dubai": "Jebel Ali, UAE",
    "khorfakkan": "Khorfakkan, UAE",
    "khor fakkan": "Khorfakkan, UAE",
    "kfk": "Khorfakkan, UAE",
    "sharjah port": "Sharjah, UAE",
    "hamriyah": "Sharjah, UAE",
    "abu dhabi port": "Abu Dhabi, UAE",
    "khalifa port": "Abu Dhabi, UAE",
    "zayed port": "Abu Dhabi, UAE",
    "fujairah": "Fujairah, UAE",
    "fujairah port": "Fujairah, UAE",
    "fuj": "Fujairah, UAE",
    "umm al quwain": "Umm Al Quwain, UAE",
    "ras al khaimah": "Ras Al Khaimah, UAE",
    "rak": "Ras Al Khaimah, UAE",

    # ── UAE — Air ──
    "dubai": "Dubai, UAE",
    "dxb": "Dubai, UAE",
    "dubai international": "Dubai, UAE",
    "dubai world central": "Dubai, UAE",
    "dwc": "Dubai, UAE",
    "sharjah": "Sharjah, UAE",
    "shj": "Sharjah, UAE",
    "abu dhabi": "Abu Dhabi, UAE",
    "auh": "Abu Dhabi, UAE",

    # ════════════════════════════════════════════════════════════
    # SAUDI ARABIA
    # ════════════════════════════════════════════════════════════
    "jeddah": "Jeddah, Saudi Arabia",
    "jeddah islamic port": "Jeddah, Saudi Arabia",
    "jid": "Jeddah, Saudi Arabia",
    "jed": "Jeddah, Saudi Arabia",
    "king abdulaziz international": "Jeddah, Saudi Arabia",
    "dammam": "Dammam, Saudi Arabia",
    "king abdulaziz port": "Dammam, Saudi Arabia",
    "damman": "Dammam, Saudi Arabia",
    "dmm": "Dammam, Saudi Arabia",
    "riyadh": "Riyadh, Saudi Arabia",
    "ruh": "Riyadh, Saudi Arabia",
    "king khalid international": "Riyadh, Saudi Arabia",
    "yanbu": "Yanbu, Saudi Arabia",
    "ybu": "Yanbu, Saudi Arabia",
    "jubail": "Jubail, Saudi Arabia",
    "king fahad industrial port": "Jubail, Saudi Arabia",
    "medina": "Medina, Saudi Arabia",
    "med": "Medina, Saudi Arabia",
    "taif": "Taif, Saudi Arabia",
    "tif": "Taif, Saudi Arabia",
    "abha": "Abha, Saudi Arabia",
    "ahl": "Abha, Saudi Arabia",
    "tabuk": "Tabuk, Saudi Arabia",
    "tuu": "Tabuk, Saudi Arabia",

    # ════════════════════════════════════════════════════════════
    # OMAN
    # ════════════════════════════════════════════════════════════
    "muscat": "Muscat, Oman",
    "mct": "Muscat, Oman",
    "sohar": "Sohar, Oman",
    "salalah": "Salalah, Oman",
    "sll": "Salalah, Oman",
    "port sultan qaboos": "Muscat, Oman",

    # ════════════════════════════════════════════════════════════
    # QATAR
    # ════════════════════════════════════════════════════════════
    "doha": "Doha, Qatar",
    "doh": "Doha, Qatar",
    "hamad port": "Doha, Qatar",
    "hamad international": "Doha, Qatar",

    # ════════════════════════════════════════════════════════════
    # KUWAIT
    # ════════════════════════════════════════════════════════════
    "kuwait": "Kuwait City, Kuwait",
    "kuwait city": "Kuwait City, Kuwait",
    "kwi": "Kuwait City, Kuwait",
    "shuaiba": "Kuwait City, Kuwait",
    "shuwaikh": "Kuwait City, Kuwait",

    # ════════════════════════════════════════════════════════════
    # BAHRAIN
    # ════════════════════════════════════════════════════════════
    "bahrain": "Manama, Bahrain",
    "manama": "Manama, Bahrain",
    "bah": "Manama, Bahrain",
    "bah port": "Manama, Bahrain",
    "khalifa bin salman": "Manama, Bahrain",
    "bah international": "Manama, Bahrain",

    # ════════════════════════════════════════════════════════════
    # CHINA — Sea Ports
    # ════════════════════════════════════════════════════════════
    "shanghai": "Shanghai, China",
    "sha": "Shanghai, China",
    "port of shanghai": "Shanghai, China",
    "yangshan": "Shanghai, China",
    "waigaoqiao": "Shanghai, China",
    "ningbo": "Ningbo, China",
    "ngb": "Ningbo, China",
    "ningbo-zhoushan": "Ningbo, China",
    "zhoushan": "Ningbo, China",
    "shenzhen": "Shenzhen, China",
    "szx": "Shenzhen, China",
    "yantian": "Shenzhen, China",
    "shekou": "Shenzhen, China",
    "chiwan": "Shenzhen, China",
    "guangzhou": "Guangzhou, China",
    "can": "Guangzhou, China",
    "canton": "Guangzhou, China",
    "nansha": "Guangzhou, China",
    "tianjin": "Tianjin, China",
    "tao": "Tianjin, China",
    "xingang": "Tianjin, China",
    "qingdao": "Qingdao, China",
    "tao port": "Qingdao, China",
    "tsingtao": "Qingdao, China",
    "tao qingdao": "Qingdao, China",
    "dalian": "Dalian, China",
    "dlc": "Dalian, China",
    "xiamen": "Xiamen, China",
    "xmn": "Xiamen, China",
    "amoy": "Xiamen, China",
    "lianyungang": "Lianyungang, China",
    "lyg": "Lianyungang, China",
    "nanjing": "Nanjing, China",
    "nkg": "Nanjing, China",
    "wuhan": "Wuhan, China",
    "wuh": "Wuhan, China",
    "chongqing": "Chongqing, China",
    "ckg": "Chongqing, China",
    "foshan": "Foshan, China",
    "dongguan": "Dongguan, China",
    "zhongshan": "Zhongshan, China",
    "zhuhai": "Zhuhai, China",
    "zhuh": "Zhuhai, China",
    "fuzhou": "Fuzhou, China",
    "foc": "Fuzhou, China",
    "wenzhou": "Wenzhou, China",
    "wuz": "Wenzhou, China",
    "nantong": "Nantong, China",
    "ntg": "Nantong, China",
    "yiwu": "Yiwu, China",
    "yiw": "Yiwu, China",

    # ── China — Air ──
    "beijing": "Beijing, China",
    "pek": "Beijing, China",
    "peking": "Beijing, China",
    "beijing capital": "Beijing, China",
    "shenzhen airport": "Shenzhen, China",
    "guangzhou baiyun": "Guangzhou, China",
    "chengdu": "Chengdu, China",
    "ctu": "Chengdu, China",
    "hong kong": "Hong Kong",
    "hkg": "Hong Kong",
    "hk": "Hong Kong",
    "hongkong": "Hong Kong",
    "kwai tsing": "Hong Kong",
    "kwai chung": "Hong Kong",

    # ════════════════════════════════════════════════════════════
    # SOUTHEAST ASIA
    # ════════════════════════════════════════════════════════════

    # Singapore
    "singapore": "Singapore",
    "sin": "Singapore",
    "sgp": "Singapore",
    "psa singapore": "Singapore",
    "port of singapore": "Singapore",
    "tanjong pagar": "Singapore",
    "pasir panjang": "Singapore",

    # Malaysia
    "port klang": "Port Klang, Malaysia",
    "klang": "Port Klang, Malaysia",
    "northport": "Port Klang, Malaysia",
    "westports": "Port Klang, Malaysia",
    "pkw": "Port Klang, Malaysia",
    "penang": "Penang, Malaysia",
    "pgu": "Penang, Malaysia",
    "butterworth": "Penang, Malaysia",
    "tanjung pelepas": "Tanjung Pelepas, Malaysia",
    "ptp": "Tanjung Pelepas, Malaysia",
    "johor bahru": "Johor Bahru, Malaysia",
    "johor port": "Johor Bahru, Malaysia",
    "kuala lumpur": "Kuala Lumpur, Malaysia",
    "kul": "Kuala Lumpur, Malaysia",
    "klia": "Kuala Lumpur, Malaysia",

    # Thailand
    "bangkok": "Bangkok, Thailand",
    "bkk": "Bangkok, Thailand",
    "laem chabang": "Laem Chabang, Thailand",
    "lcb": "Laem Chabang, Thailand",
    "map ta phut": "Map Ta Phut, Thailand",

    # Vietnam
    "ho chi minh city": "Ho Chi Minh City, Vietnam",
    "saigon": "Ho Chi Minh City, Vietnam",
    "hcm": "Ho Chi Minh City, Vietnam",
    "sgn": "Ho Chi Minh City, Vietnam",
    "cat lai": "Ho Chi Minh City, Vietnam",
    "hanoi": "Hanoi, Vietnam",
    "han": "Hanoi, Vietnam",
    "hai phong": "Hai Phong, Vietnam",
    "haiphong": "Hai Phong, Vietnam",
    "hph": "Hai Phong, Vietnam",
    "da nang": "Da Nang, Vietnam",
    "dad": "Da Nang, Vietnam",

    # Indonesia
    "jakarta": "Jakarta, Indonesia",
    "jkt": "Jakarta, Indonesia",
    "tanjung priok": "Jakarta, Indonesia",
    "priok": "Jakarta, Indonesia",
    "surabaya": "Surabaya, Indonesia",
    "sub": "Surabaya, Indonesia",
    "tanjung perak": "Surabaya, Indonesia",
    "bali": "Bali, Indonesia",
    "dps": "Bali, Indonesia",
    "denpasar": "Bali, Indonesia",
    "medan": "Medan, Indonesia",
    "mdn": "Medan, Indonesia",
    "belawan": "Medan, Indonesia",

    # Philippines
    "manila": "Manila, Philippines",
    "mnl": "Manila, Philippines",
    "port of manila": "Manila, Philippines",
    "manila international container": "Manila, Philippines",
    "cebu": "Cebu, Philippines",
    "ceb": "Cebu, Philippines",

    # Myanmar
    "yangon": "Yangon, Myanmar",
    "rangoon": "Yangon, Myanmar",
    "yan": "Yangon, Myanmar",

    # Cambodia
    "phnom penh": "Phnom Penh, Cambodia",
    "pnh": "Phnom Penh, Cambodia",
    "sihanoukville": "Sihanoukville, Cambodia",

    # ════════════════════════════════════════════════════════════
    # SOUTH ASIA
    # ════════════════════════════════════════════════════════════

    # Pakistan
    "karachi": "Karachi, Pakistan",
    "khi": "Karachi, Pakistan",
    "port qasim": "Karachi, Pakistan",
    "lahore": "Lahore, Pakistan",
    "lhe": "Lahore, Pakistan",
    "islamabad": "Islamabad, Pakistan",
    "isb": "Islamabad, Pakistan",

    # Bangladesh
    "chittagong": "Chittagong, Bangladesh",
    "cgp": "Chittagong, Bangladesh",
    "dhaka": "Dhaka, Bangladesh",
    "dac": "Dhaka, Bangladesh",

    # Sri Lanka
    "colombo": "Colombo, Sri Lanka",
    "cmb": "Colombo, Sri Lanka",
    "jcty": "Colombo, Sri Lanka",

    # Nepal
    "kathmandu": "Kathmandu, Nepal",
    "ktm": "Kathmandu, Nepal",

    # ════════════════════════════════════════════════════════════
    # EUROPE — Sea Ports
    # ════════════════════════════════════════════════════════════

    # Germany
    "hamburg": "Hamburg, Germany",
    "ham": "Hamburg, Germany",
    "hafen hamburg": "Hamburg, Germany",
    "bremen": "Bremen, Germany",
    "brv": "Bremen, Germany",
    "bremerhaven": "Bremerhaven, Germany",
    "bhv": "Bremerhaven, Germany",
    "duisburg": "Duisburg, Germany",

    # Netherlands
    "rotterdam": "Rotterdam, Netherlands",
    "rtm": "Rotterdam, Netherlands",
    "port of rotterdam": "Rotterdam, Netherlands",
    "amsterdam": "Amsterdam, Netherlands",
    "ams": "Amsterdam, Netherlands",

    # Belgium
    "antwerp": "Antwerp, Belgium",
    "ant": "Antwerp, Belgium",
    "port of antwerp": "Antwerp, Belgium",
    "antwerpen": "Antwerp, Belgium",
    "zeebrugge": "Zeebrugge, Belgium",
    "zee": "Zeebrugge, Belgium",
    "brussels": "Brussels, Belgium",
    "bru": "Brussels, Belgium",

    # UK
    "london": "London, UK",
    "lon": "London, UK",
    "heathrow": "London, UK",
    "lhr": "London, UK",
    "felixstowe": "Felixstowe, UK",
    "fxt": "Felixstowe, UK",
    "southampton": "Southampton, UK",
    "sou": "Southampton, UK",
    "liverpool": "Liverpool, UK",
    "lvp": "Liverpool, UK",
    "tilbury": "Tilbury, UK",
    "manchester": "Manchester, UK",
    "man": "Manchester, UK",

    # France
    "le havre": "Le Havre, France",
    "leh": "Le Havre, France",
    "havre": "Le Havre, France",
    "paris": "Paris, France",
    "cdg": "Paris, France",
    "charles de gaulle": "Paris, France",
    "marseille": "Marseille, France",
    "mrs": "Marseille, France",
    "fos sur mer": "Marseille, France",
    "dunkirk": "Dunkirk, France",
    "dkk": "Dunkirk, France",
    "lyon": "Lyon, France",
    "lys": "Lyon, France",

    # Spain
    "barcelona": "Barcelona, Spain",
    "bcn": "Barcelona, Spain",
    "valencia": "Valencia, Spain",
    "vlc": "Valencia, Spain",
    "madrid": "Madrid, Spain",
    "mad": "Madrid, Spain",
    "algeciras": "Algeciras, Spain",
    "alg": "Algeciras, Spain",
    "bilbao": "Bilbao, Spain",
    "bio": "Bilbao, Spain",

    # Italy
    "genoa": "Genoa, Italy",
    "goa port italy": "Genoa, Italy",
    "gov": "Genoa, Italy",
    "genova": "Genoa, Italy",
    "milan": "Milan, Italy",
    "mxp": "Milan, Italy",
    "malpensa": "Milan, Italy",
    "rome": "Rome, Italy",
    "fco": "Rome, Italy",
    "la spezia": "La Spezia, Italy",
    "spz": "La Spezia, Italy",
    "livorno": "Livorno, Italy",
    "naples": "Naples, Italy",
    "nap": "Naples, Italy",
    "gioia tauro": "Gioia Tauro, Italy",
    "venice": "Venice, Italy",
    "vce": "Venice, Italy",
    "trieste": "Trieste, Italy",
    "try": "Trieste, Italy",

    # Greece
    "piraeus": "Piraeus, Greece",
    "pir": "Piraeus, Greece",
    "athens": "Athens, Greece",
    "ath": "Athens, Greece",
    "thessaloniki": "Thessaloniki, Greece",
    "skg": "Thessaloniki, Greece",

    # Turkey
    "istanbul": "Istanbul, Turkey",
    "ist": "Istanbul, Turkey",
    "mersin": "Mersin, Turkey",
    "mez": "Mersin, Turkey",
    "izmir": "Izmir, Turkey",
    "adb": "Izmir, Turkey",
    "ambarli": "Istanbul, Turkey",
    "haydarpasa": "Istanbul, Turkey",
    "ankara": "Ankara, Turkey",
    "esb": "Ankara, Turkey",

    # Poland
    "gdansk": "Gdansk, Poland",
    "gdn": "Gdansk, Poland",
    "gdynia": "Gdynia, Poland",
    "warsaw": "Warsaw, Poland",
    "waw": "Warsaw, Poland",

    # Scandinavia
    "gothenburg": "Gothenburg, Sweden",
    "got": "Gothenburg, Sweden",
    "stockholm": "Stockholm, Sweden",
    "arn": "Stockholm, Sweden",
    "oslo": "Oslo, Norway",
    "osl": "Oslo, Norway",
    "copenhagen": "Copenhagen, Denmark",
    "cph": "Copenhagen, Denmark",
    "helsinki": "Helsinki, Finland",
    "hel": "Helsinki, Finland",

    # Russia
    "saint petersburg": "Saint Petersburg, Russia",
    "led": "Saint Petersburg, Russia",
    "st. petersburg": "Saint Petersburg, Russia",
    "moscow": "Moscow, Russia",
    "svo": "Moscow, Russia",
    "vladivostok": "Vladivostok, Russia",
    "vvo": "Vladivostok, Russia",
    "novorossiysk": "Novorossiysk, Russia",

    # ════════════════════════════════════════════════════════════
    # USA — Sea Ports
    # ════════════════════════════════════════════════════════════
    "los angeles": "Los Angeles, USA",
    "lax": "Los Angeles, USA",
    "la": "Los Angeles, USA",
    "long beach": "Long Beach, USA",
    "lgb": "Long Beach, USA",
    "new york": "New York, USA",
    "nyc": "New York, USA",
    "jfk": "New York, USA",
    "newark": "New York, USA",
    "ewr": "New York, USA",
    "port newark": "New York, USA",
    "houston": "Houston, USA",
    "hou": "Houston, USA",
    "iah": "Houston, USA",
    "port of houston": "Houston, USA",
    "savannah": "Savannah, USA",
    "sav": "Savannah, USA",
    "charleston": "Charleston, USA",
    "chs": "Charleston, USA",
    "seattle": "Seattle, USA",
    "sea": "Seattle, USA",
    "tacoma": "Tacoma, USA",
    "tiw": "Tacoma, USA",
    "miami": "Miami, USA",
    "mia": "Miami, USA",
    "port of miami": "Miami, USA",
    "baltimore": "Baltimore, USA",
    "bwi": "Baltimore, USA",
    "norfolk": "Norfolk, USA",
    "orf": "Norfolk, USA",
    "chicago": "Chicago, USA",
    "ord": "Chicago, USA",
    "o'hare": "Chicago, USA",
    "new orleans": "New Orleans, USA",
    "msy": "New Orleans, USA",
    "dallas": "Dallas, USA",
    "dfw": "Dallas, USA",
    "atlanta": "Atlanta, USA",
    "atl": "Atlanta, USA",
    "san francisco": "San Francisco, USA",
    "sfo": "San Francisco, USA",
    "boston": "Boston, USA",
    "bos": "Boston, USA",

    # ════════════════════════════════════════════════════════════
    # CANADA
    # ════════════════════════════════════════════════════════════
    "vancouver": "Vancouver, Canada",
    "yvr": "Vancouver, Canada",
    "prince rupert": "Prince Rupert, Canada",
    "ypr": "Prince Rupert, Canada",
    "toronto": "Toronto, Canada",
    "yyz": "Toronto, Canada",
    "montreal": "Montreal, Canada",
    "yul": "Montreal, Canada",
    "halifax": "Halifax, Canada",
    "yhz": "Halifax, Canada",

    # ════════════════════════════════════════════════════════════
    # AUSTRALIA & NEW ZEALAND
    # ════════════════════════════════════════════════════════════
    "sydney": "Sydney, Australia",
    "syd": "Sydney, Australia",
    "melbourne": "Melbourne, Australia",
    "mel": "Melbourne, Australia",
    "brisbane": "Brisbane, Australia",
    "bne": "Brisbane, Australia",
    "fremantle": "Fremantle, Australia",
    "perth": "Perth, Australia",
    "per": "Perth, Australia",
    "adelaide": "Adelaide, Australia",
    "adl": "Adelaide, Australia",
    "auckland": "Auckland, New Zealand",
    "akl": "Auckland, New Zealand",

    # ════════════════════════════════════════════════════════════
    # EAST AFRICA
    # ════════════════════════════════════════════════════════════
    "mombasa": "Mombasa, Kenya",
    "mom": "Mombasa, Kenya",
    "nairobi": "Nairobi, Kenya",
    "nbo": "Nairobi, Kenya",
    "dar es salaam": "Dar es Salaam, Tanzania",
    "dares": "Dar es Salaam, Tanzania",
    "dar": "Dar es Salaam, Tanzania",
    "addis ababa": "Addis Ababa, Ethiopia",
    "add": "Addis Ababa, Ethiopia",
    "djibouti": "Djibouti",
    "jib": "Djibouti",
    "port of djibouti": "Djibouti",
    "kampala": "Kampala, Uganda",
    "entebbe": "Kampala, Uganda",
    "ebb": "Kampala, Uganda",
    "kigali": "Kigali, Rwanda",
    "kgl": "Kigali, Rwanda",
    "antananarivo": "Antananarivo, Madagascar",
    "tnr": "Antananarivo, Madagascar",
    "tamatave": "Toamasina, Madagascar",

    # ════════════════════════════════════════════════════════════
    # WEST AFRICA
    # ════════════════════════════════════════════════════════════
    "lagos": "Lagos, Nigeria",
    "los": "Lagos, Nigeria",
    "apapa": "Lagos, Nigeria",
    "tin can island": "Lagos, Nigeria",
    "abuja": "Abuja, Nigeria",
    "abv": "Abuja, Nigeria",
    "accra": "Accra, Ghana",
    "acc": "Accra, Ghana",
    "tema": "Accra, Ghana",
    "abidjan": "Abidjan, Ivory Coast",
    "abj": "Abidjan, Ivory Coast",
    "dakar": "Dakar, Senegal",
    "dkr": "Dakar, Senegal",

    # ════════════════════════════════════════════════════════════
    # SOUTH AFRICA
    # ════════════════════════════════════════════════════════════
    "durban": "Durban, South Africa",
    "dur": "Durban, South Africa",
    "cape town": "Cape Town, South Africa",
    "cpt": "Cape Town, South Africa",
    "johannesburg": "Johannesburg, South Africa",
    "jnb": "Johannesburg, South Africa",
    "port elizabeth": "Port Elizabeth, South Africa",
    "plz": "Port Elizabeth, South Africa",
    "gqeberha": "Port Elizabeth, South Africa",

    # ════════════════════════════════════════════════════════════
    # EGYPT & NORTH AFRICA
    # ════════════════════════════════════════════════════════════
    "port said": "Port Said, Egypt",
    "psd": "Port Said, Egypt",
    "alexandria": "Alexandria, Egypt",
    "ale": "Alexandria, Egypt",
    "cairo": "Cairo, Egypt",
    "cai": "Cairo, Egypt",
    "suez": "Suez, Egypt",
    "damietta": "Damietta, Egypt",
    "dam": "Damietta, Egypt",
    "casablanca": "Casablanca, Morocco",
    "cmn": "Casablanca, Morocco",
    "tanger med": "Tangier, Morocco",
    "tangier": "Tangier, Morocco",
    "tunis": "Tunis, Tunisia",
    "tun": "Tunis, Tunisia",
    "tripoli": "Tripoli, Libya",
    "tip": "Tripoli, Libya",

    # ════════════════════════════════════════════════════════════
    # JAPAN & KOREA
    # ════════════════════════════════════════════════════════════
    "tokyo": "Tokyo, Japan",
    "tyo": "Tokyo, Japan",
    "narita": "Tokyo, Japan",
    "nrt": "Tokyo, Japan",
    "yokohama": "Yokohama, Japan",
    "yok": "Yokohama, Japan",
    "osaka": "Osaka, Japan",
    "kix": "Osaka, Japan",
    "kansai": "Osaka, Japan",
    "nagoya": "Nagoya, Japan",
    "ngo": "Nagoya, Japan",
    "kobe": "Kobe, Japan",
    "uky": "Kobe, Japan",
    "busan": "Busan, South Korea",
    "pus": "Busan, South Korea",
    "pusan": "Busan, South Korea",
    "seoul": "Seoul, South Korea",
    "icn": "Seoul, South Korea",
    "incheon": "Seoul, South Korea",

    # ════════════════════════════════════════════════════════════
    # TAIWAN
    # ════════════════════════════════════════════════════════════
    "taipei": "Taipei, Taiwan",
    "tpe": "Taipei, Taiwan",
    "kaohsiung": "Kaohsiung, Taiwan",
    "khh": "Kaohsiung, Taiwan",
    "taichung": "Taichung, Taiwan",
    "rmq": "Taichung, Taiwan",

    # ════════════════════════════════════════════════════════════
    # MIDDLE EAST — OTHER
    # ════════════════════════════════════════════════════════════
    "beirut": "Beirut, Lebanon",
    "bey": "Beirut, Lebanon",
    "amman": "Amman, Jordan",
    "amm": "Amman, Jordan",
    "aqaba": "Aqaba, Jordan",
    "aqj": "Aqaba, Jordan",
    "baghdad": "Baghdad, Iraq",
    "bgh": "Baghdad, Iraq",
    "basra": "Basra, Iraq",
    "bsr": "Basra, Iraq",
    "umm qasr": "Basra, Iraq",
    "tehran": "Tehran, Iran",
    "thr": "Tehran, Iran",
    "bandar abbas": "Bandar Abbas, Iran",
    "bnd": "Bandar Abbas, Iran",
    "tel aviv": "Tel Aviv, Israel",
    "tlv": "Tel Aviv, Israel",
    "haifa": "Haifa, Israel",
    "hfa": "Haifa, Israel",
    "ashdod": "Ashdod, Israel",

    # ════════════════════════════════════════════════════════════
    # LATIN AMERICA
    # ════════════════════════════════════════════════════════════
    "santos": "Santos, Brazil",
    "sao paulo": "Sao Paulo, Brazil",
    "gru": "Sao Paulo, Brazil",
    "guarulhos": "Sao Paulo, Brazil",
    "rio de janeiro": "Rio de Janeiro, Brazil",
    "gig": "Rio de Janeiro, Brazil",
    "buenos aires": "Buenos Aires, Argentina",
    "eze": "Buenos Aires, Argentina",
    "ezeiza": "Buenos Aires, Argentina",
    "callao": "Callao, Peru",
    "lima": "Lima, Peru",
    "lim": "Lima, Peru",
    "valparaiso": "Valparaiso, Chile",
    "santiago": "Santiago, Chile",
    "scl": "Santiago, Chile",
    "bogota": "Bogota, Colombia",
    "bog": "Bogota, Colombia",
    "cartagena": "Cartagena, Colombia",
    "ctg": "Cartagena, Colombia",
    "manzanillo": "Manzanillo, Mexico",
    "mzt": "Manzanillo, Mexico",
    "mexico city": "Mexico City, Mexico",
    "mex": "Mexico City, Mexico",
    "veracruz": "Veracruz, Mexico",
    "ver": "Veracruz, Mexico",
    "colon": "Colon, Panama",
    "manzanillo panama": "Colon, Panama",
    "panama city": "Panama City, Panama",
    "pty": "Panama City, Panama",

    # ════════════════════════════════════════════════════════════
    # CENTRAL ASIA & CAUCASUS
    # ════════════════════════════════════════════════════════════
    "almaty": "Almaty, Kazakhstan",
    "ala": "Almaty, Kazakhstan",
    "nur-sultan": "Astana, Kazakhstan",
    "astana": "Astana, Kazakhstan",
    "tashkent": "Tashkent, Uzbekistan",
    "tas": "Tashkent, Uzbekistan",
    "baku": "Baku, Azerbaijan",
    "gyd": "Baku, Azerbaijan",
    "tbilisi": "Tbilisi, Georgia",
    "tbs": "Tbilisi, Georgia",
    "yerevan": "Yerevan, Armenia",
    "evn": "Yerevan, Armenia",
}

# Build a sorted list of aliases (longest first) for greedy prefix matching
_SORTED_ALIASES = sorted(_ALIASES.keys(), key=len, reverse=True)


def normalize_port(name: str) -> str:
    """
    Normalize a port/city name to its canonical form.

    Examples:
        normalize_port("JNPT")           → "Nhava Sheva, India"
        normalize_port("Jebel Ali Port") → "Jebel Ali, UAE"
        normalize_port("NGB")            → "Ningbo, China"
        normalize_port("Unknown Place")  → "Unknown Place"  (unchanged)

    Args:
        name: Raw port/city string from email or user input.

    Returns:
        Canonical name if alias found, otherwise the original string stripped.
    """
    if not name:
        return name

    cleaned = name.strip().lower()

    # Exact match first
    if cleaned in _ALIASES:
        return _ALIASES[cleaned]

    # Substring match — longest alias wins (prevents "dubai" matching inside "dubai international airport")
    for alias in _SORTED_ALIASES:
        if alias in cleaned:
            return _ALIASES[alias]

    # No match — return original with title casing preserved
    return name.strip()


def normalize_shipment(shipment: dict) -> dict:
    """
    Normalize origin and destination fields of a shipment dict.
    Returns a new dict (immutable — does not mutate the original).

    Args:
        shipment: Dict with at minimum 'origin' and 'destination' keys.

    Returns:
        New dict with normalized origin/destination.
    """
    return {
        **shipment,
        "origin": normalize_port(shipment.get("origin", "")),
        "destination": normalize_port(shipment.get("destination", "")),
    }


if __name__ == "__main__":
    tests = [
        # India
        ("JNPT", "Nhava Sheva, India"),
        ("Jawaharlal Nehru Port", "Nhava Sheva, India"),
        ("Mumbai", "Nhava Sheva, India"),
        ("Mundra", "Mundra Port, India"),
        ("VIZAG", "Visakhapatnam, India"),
        ("BLR", "Bangalore, India"),
        # UAE
        ("Jebel Ali Port", "Jebel Ali, UAE"),
        ("JEA", "Jebel Ali, UAE"),
        ("Dubai", "Dubai, UAE"),
        ("DXB", "Dubai, UAE"),
        ("Khor Fakkan", "Khorfakkan, UAE"),
        # Saudi
        ("jeddah islamic port", "Jeddah, Saudi Arabia"),
        ("DMM", "Dammam, Saudi Arabia"),
        # China
        ("NGB", "Ningbo, China"),
        ("Ningbo-Zhoushan", "Ningbo, China"),
        ("SHA", "Shanghai, China"),
        ("Yangshan", "Shanghai, China"),
        ("Yantian", "Shenzhen, China"),
        ("Shekou", "Shenzhen, China"),
        ("HKG", "Hong Kong"),
        ("Guangzhou Nansha", "Guangzhou, China"),
        # Europe
        ("Hamburg", "Hamburg, Germany"),
        ("Rotterdam", "Rotterdam, Netherlands"),
        ("Antwerp", "Antwerp, Belgium"),
        ("Felixstowe", "Felixstowe, UK"),
        ("Le Havre", "Le Havre, France"),
        ("Piraeus", "Piraeus, Greece"),
        # USA
        ("Los Angeles", "Los Angeles, USA"),
        ("Long Beach", "Long Beach, USA"),
        ("JFK", "New York, USA"),
        ("Houston", "Houston, USA"),
        # SE Asia
        ("Singapore", "Singapore"),
        ("Port Klang", "Port Klang, Malaysia"),
        ("Laem Chabang", "Laem Chabang, Thailand"),
        ("Tanjung Priok", "Jakarta, Indonesia"),
        # Africa
        ("Mombasa", "Mombasa, Kenya"),
        ("Durban", "Durban, South Africa"),
        ("Port Said", "Port Said, Egypt"),
        ("Lagos", "Lagos, Nigeria"),
        # Unknown
        ("SomeUnknownPort", "SomeUnknownPort"),
    ]

    print(f"Port normalization tests ({len(tests)} cases):")
    passed = 0
    for raw, expected in tests:
        result = normalize_port(raw)
        ok = result == expected
        if ok:
            passed += 1
        status = "✓" if ok else "✗"
        print(f"  {status} '{raw}' → '{result}'" + ("" if ok else f"  (expected: '{expected}')"))

    print(f"\n{passed}/{len(tests)} passed")
