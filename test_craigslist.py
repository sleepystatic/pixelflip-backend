import requests
from bs4 import BeautifulSoup

print('Testing Craigslist...')

url = 'https://stockton.craigslist.org/search/vga?query=nintendo+switch'
response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'})

print(f'Status: {response.status_code}')

soup = BeautifulSoup(response.content, 'html.parser')
items = soup.find_all('li', class_='cl-static-search-result')

print(f'Found {len(items)} items with class cl-static-search-result')

if items:
    print('\nFirst 3 items:')
    for item in items[:3]:
        # Extract title
        title = item.find('div', class_='title')
        title_text = title.text.strip() if title else "No title"

        # Extract price
        price = item.find('div', class_='price')
        price_text = price.text.strip() if price else "No price"

        # Extract link
        link = item.find('a')
        link_url = link['href'] if link else "No link"
        if link_url and not link_url.startswith('http'):
            link_url = 'https://stockton.craigslist.org' + link_url

        print(f'  {title_text} | {price_text}')
        print(f'  Link: {link_url}\n')
else:
    print('\nNo items found')