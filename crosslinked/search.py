import logging
import threading
from time import sleep
from random import choice, uniform, randint
from bs4 import BeautifulSoup
from unidecode import unidecode
from urllib.parse import urlparse, quote_plus
from crosslinked.logger import Log
from datetime import datetime, timedelta

# Try to import curl_cffi first (fixes JA4/JA3 TLS fingerprinting)
try:
    from curl_cffi import requests
    from curl_cffi.requests import RequestsError
    CURL_CFFI_AVAILABLE = True
    Log.success("Using curl_cffi - TLS fingerprinting protection enabled")
except ImportError:
    import requests
    from requests.exceptions import RequestException as RequestsError
    from urllib3 import disable_warnings, exceptions
    disable_warnings(exceptions.InsecureRequestWarning)
    CURL_CFFI_AVAILABLE = False
    Log.warn("curl_cffi not installed - TLS fingerprinting may cause blocks")
    Log.warn("Install with: pip install curl_cffi")

# Disable SSL warnings
if not CURL_CFFI_AVAILABLE:
    logging.getLogger("urllib3").setLevel(logging.WARNING)

csv = logging.getLogger('cLinked_csv')


def build_google_query(company, start_offset):
    """
    Build varied Google search queries to avoid detection patterns.
    Randomizes query structure while maintaining functionality.
    """
    # Different query structures that produce same results
    query_templates = [
        # Standard format
        'site:linkedin.com/in "{company}"',
        # Reverse order
        '"{company}" site:linkedin.com/in',
        # With OR operator (same company twice, but looks different)
        'site:linkedin.com/in ("{company}" OR "{company}")',
        # With wildcard (still matches exact)
        'site:linkedin.com/in "{company}"*',
        # With intitle (LinkedIn profiles have company in title)
        'site:linkedin.com/in intitle:"{company}"',
        # Multiple site variations (all same)
        '(site:linkedin.com/in OR site:www.linkedin.com/in) "{company}"',
        # With inurl (redundant but valid)
        'site:linkedin.com/in inurl:in "{company}"',
    ]
    
    query = choice(query_templates).format(company=company)
    
    # Randomize parameter order
    params = []
    
    # Add num parameter with slight variation
    num_results = choice([100, 99, 98])  # Google treats these similarly
    params.append(('num', num_results))
    
    # Add start parameter
    params.append(('start', start_offset))
    
    # Occasionally add extra parameters that don't affect results
    if randint(0, 2) == 0:
        # Add filter parameter (default is 1 anyway)
        params.append(('filter', 1))
    
    if randint(0, 2) == 0:
        # Add safe search (default is off)
        params.append(('safe', 'off'))
    
    # Randomize parameter order
    from random import shuffle
    shuffle(params)
    
    # Build URL with randomized parameter order
    param_str = '&'.join([f'{k}={v}' for k, v in params])
    url = f'https://www.google.com/search?q={quote_plus(query)}&{param_str}'
    
    return url


def build_bing_query(company, start_offset):
    """
    Build varied Bing search queries to avoid detection patterns.
    """
    query_templates = [
        '"{company}" site:linkedin.com/in',
        'site:linkedin.com/in "{company}"',
        '("{company}") site:linkedin.com/in',
        'site:linkedin.com/in ("{company}")',
    ]
    
    query = choice(query_templates).format(company=company)
    
    # Bing uses 'first' parameter for pagination
    url = f'http://www.bing.com/search?q={quote_plus(query)}&first={start_offset}'
    
    return url


class Timer(threading.Thread):
    def __init__(self, timeout):
        threading.Thread.__init__(self)
        self.start_time = None
        self.running = None
        self.timeout = timeout

    def run(self):
        self.running = True
        self.start_time = datetime.now()
        logging.debug("Thread Timer: Started")

        while self.running:
            if (datetime.now() - self.start_time) > timedelta(seconds=self.timeout):
                self.stop()
            sleep(0.05)

    def stop(self):
        logging.debug("Thread Timer: Stopped")
        self.running = False


class CrossLinked:
    def __init__(self, search_engine, target, timeout, conn_timeout=3, proxies=[], jitter=0):
        self.results = []
        # Keep old URL templates for compatibility, but will use new builders
        self.url = {'google': 'https://www.google.com/search?q=site:linkedin.com/in+"{}"&num=100&start={}',
                    'bing': 'http://www.bing.com/search?q="{}"+site:linkedin.com/in&first={}'}

        self.runtime = datetime.now().strftime('%m-%d-%Y %H:%M:%S')
        self.search_engine = search_engine
        self.conn_timeout = conn_timeout
        self.timeout = timeout
        self.proxies = proxies
        self.target = target
        self.jitter = jitter
        self.user_agent = get_agent()  # Pick one user agent per session
        self.request_count = 0  # Track requests for behavior variation

    def search(self):
        search_timer = Timer(self.timeout)
        search_timer.start()

        while search_timer.running:
            try:
                # Build query with randomization
                if self.search_engine == 'google':
                    url = build_google_query(self.target, len(self.results))
                elif self.search_engine == 'bing':
                    url = build_bing_query(self.target, len(self.results))
                else:
                    # Fallback to old method
                    url = self.url[self.search_engine].format(self.target, len(self.results))
                
                # Make request with session-consistent user agent
                resp = web_request(url, self.conn_timeout, self.proxies, user_agent=self.user_agent)
                http_code = get_statuscode(resp)

                if http_code != 200:
                    Log.info("{:<3} {} ({})".format(len(self.results), url, http_code))
                    Log.warn('None 200 response, exiting search ({})'.format(http_code))
                    break

                self.page_parser(resp)
                Log.info("{:<3} {} ({})".format(len(self.results), url, http_code))

                # Variable delay that increases slightly with each request (mimics human fatigue)
                self.request_count += 1
                base_jitter = self.jitter if self.jitter > 0 else 1
                # Add random variation ±30% and slight increase over time
                jitter_variation = uniform(base_jitter * 0.7, base_jitter * 1.3)
                fatigue_factor = 1 + (self.request_count * 0.02)  # 2% slower each request
                actual_delay = jitter_variation * fatigue_factor
                
                logging.debug(f"Sleeping for {actual_delay:.2f} seconds (request #{self.request_count})")
                sleep(actual_delay)
                
            except KeyboardInterrupt:
                Log.warn("Key event detected, exiting search...")
                break

        search_timer.stop()
        return self.results

    def page_parser(self, resp):
        for link in extract_links(resp):
            try:
                self.results_handler(link)
            except Exception as e:
                Log.warn('Failed Parsing: {}- {}'.format(link.get('href'), e))

    def link_parser(self, url, link):
        u = {'url': url}
        u['text'] = unidecode(link.text.split("|")[0].split("...")[0])  # Capture link text before trailing chars
        u['title'] = self.parse_linkedin_title(u['text'])               # Extract job title
        u['name'] = self.parse_linkedin_name(u['text'])                 # Extract whole name
        return u

    def parse_linkedin_title(self, data):
        try:
            title = data.split("-")[1].split('https:')[0]
            return title.split("...")[0].split("|")[0].strip()
        except:
            return 'N/A'

    def parse_linkedin_name(self, data):
        try:
            name = data.split("-")[0].strip()
            return unidecode(name).lower()
        except:
            return False

    def results_handler(self, link):
        url = str(link.get('href')).lower()

        if not extract_subdomain(url).endswith('linkedin.com'):
            return False
        elif 'linkedin.com/in' not in url:
            return False

        data = self.link_parser(url, link)
        self.log_results(data) if data['name'] else False


    def log_results(self, d):
        # Prevent Duplicates & non-standard responses (i.e: "<span>linkedin.com</span></a>")
        if d in self.results:
            return
        elif 'linkedin.com' in d['name']:
            return

        self.results.append(d)
        # Search results are logged to names.csv but names.txt is not generated until end to prevent duplicates
        logging.debug('name: {:25} RawTxt: {}'.format(d['name'], d['text']))
        csv.info('"{}","{}","{}","{}","{}","{}",'.format(self.runtime, self.search_engine, d['name'], d['title'], d['url'], d['text']))


def get_statuscode(resp):
    try:
        return resp.status_code
    except:
        return 0


def get_proxy(proxies):
    tmp = choice(proxies) if proxies else False
    return {"http": tmp, "https": tmp} if tmp else {}


def get_agent():
    return choice([
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 14.7; rv:132.0) Gecko/20100101 Firefox/132.0',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0'
    ])


def get_varied_headers(user_agent=None):
    """
    Generate varied but realistic browser headers.
    Randomizes some values to avoid fingerprinting.
    """
    if user_agent is None:
        user_agent = get_agent()
    
    # Vary Accept-Language slightly
    accept_languages = [
        'en-US,en;q=0.9',
        'en-US,en;q=0.9,es;q=0.8',
        'en-US,en;q=0.9,fr;q=0.8',
        'en-GB,en;q=0.9,en-US;q=0.8',
        'en-US,en;q=0.8',
    ]
    
    # Vary Accept header slightly
    accept_headers = [
        'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
        'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    ]
    
    headers = {
        'User-Agent': user_agent,
        'Accept': choice(accept_headers),
        'Accept-Language': choice(accept_languages),
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
    }
    
    # Randomly include/exclude some optional headers
    if randint(0, 1):
        headers['DNT'] = '1'
    
    if randint(0, 2) == 0:
        headers['Cache-Control'] = 'max-age=0'
    
    # Randomly add viewport width (realistic browser behavior)
    if randint(0, 3) == 0:
        widths = [1920, 1680, 1440, 1366, 1280, 1024]
        headers['Viewport-Width'] = str(choice(widths))
    
    return headers


def web_request(url, timeout=3, proxies=[], user_agent=None, **kwargs):
    try:
        # Get varied headers
        headers = get_varied_headers(user_agent)
        
        # Vary cookie slightly
        consent_variations = [
            'YES+cb.20210720-07-p0.en+FX+410',
            'YES+cb.20210720-07-p0.en+FX+411',
            'YES+cb.20210720-07-p0.en+FX+412',
            'YES+cb',
        ]
        cookies = {'CONSENT': choice(consent_variations)}
        
        # Occasionally add SOCS cookie (another Google cookie)
        if randint(0, 2) == 0:
            cookies['SOCS'] = 'CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg'
        
        # Use curl_cffi if available (fixes JA4/JA3 TLS fingerprinting)
        if CURL_CFFI_AVAILABLE:
            # Pick random browser to impersonate (mimics real browser TLS fingerprint)
            browsers = [
                'chrome131',
                'chrome130', 
                'chrome129',
                'firefox132',
                'firefox131',
                'safari17_0',
                'safari17_2',
                'edge131',
                'edge130'
            ]
            impersonate = choice(browsers)
            
            logging.debug(f"Using curl_cffi with impersonate={impersonate}")
            
            # Build proxies dict for curl_cffi
            proxy_dict = get_proxy(proxies)
            
            response = requests.get(
                url,
                headers=headers,
                cookies=cookies,
                timeout=timeout,
                proxies=proxy_dict if proxy_dict else None,
                verify=False,
                impersonate=impersonate,  # This fixes JA4 fingerprinting!
                **kwargs
            )
        else:
            # Fallback to regular requests (will likely be blocked by JA4 detection)
            s = requests.Session()
            r = requests.Request('GET', url, headers=headers, cookies=cookies, **kwargs)
            p = r.prepare()
            response = s.send(p, timeout=timeout, verify=False, proxies=get_proxy(proxies))
        
        return response
        
    except RequestsError as e:
        if 'TooManyRedirects' in str(type(e).__name__):
            Log.fail('Proxy Error: {}'.format(e))
        else:
            logging.debug(f'Request error: {e}')
    except Exception as e:
        logging.debug(f'Unexpected error: {e}')
    
    return False


def extract_links(resp):
    links = []
    soup = BeautifulSoup(resp.content, 'lxml')
    for link in soup.findAll('a'):
        links.append(link)
    return links


def extract_subdomain(url):
    return urlparse(url).netloc
