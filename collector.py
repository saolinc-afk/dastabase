# Dastabase - collector v0.6.1
import random
import re
from playwright.sync_api import sync_playwright
from database import initialize_database, save_company

BASE_URL="https://www.gvin.com"
CDP_URL="http://127.0.0.1:9222"

POSTBACK_JS = "__doPostBack('ctl00$contentPlaceHolderMain$ctl00$ctl04$bamSearch$ctl21$bamSearch_lnkPagingNext','')"


def human_pause(page):
    r=random.random()
    if r<0.7: ms=random.randint(800,1500)
    elif r<0.9: ms=random.randint(2000,4000)
    else: ms=random.randint(5000,8000)
    page.wait_for_timeout(ms)

def parse_number(v):
    v=v.strip()
    if not v or v.lower()=="n.p.":
        return 0.0
    v=v.replace(".","").replace(",",".")
    try:
        return round(float(v),2)
    except:
        return 0.0

def find_gvin_page(ctx):
    for p in ctx.pages:
        if "gvin" in p.url.lower():
            return p
    return None

def get_links_on_page(page):
    out=[]
    for i in range(page.locator("a").count()):
        try:
            href=page.locator("a").nth(i).get_attribute("href")
            if href and "Company.aspx?CompanyId=" in href:
                out.append(href)
        except:
            pass
    return list(dict.fromkeys(out))

def get_all_company_links(page):
    all_links=[]
    seen_first=set()
    page_no=1
    while True:
        links=get_links_on_page(page)
        print(f"Page {page_no}: {len(links)} companies")
        all_links.extend(links)

        sig=tuple(links[:3])
        if sig in seen_first:
            print("Reached last page.")
            break
        seen_first.add(sig)

        try:
            page.evaluate(POSTBACK_JS)
            page.wait_for_timeout(1800)
            new_links=get_links_on_page(page)
            if new_links[:3]==list(sig):
                break
            page_no+=1
        except Exception:
            break
    return list(dict.fromkeys(all_links))

def financial_values(page):
    r={"revenue_2024":0.0,"revenue_2025":0.0,"employees_2024":0.0,"employees_2025":0.0}
    rows=page.locator("table.datatable tbody tr")
    for i in range(rows.count()):
        c=rows.nth(i).locator("td")
        if c.count()<6: continue
        t=c.nth(0).inner_text().lower()
        if t.startswith("celotni prihod") or t.startswith("prihodki obrest"):
            r["revenue_2024"]=parse_number(c.nth(4).inner_text())
            r["revenue_2025"]=parse_number(c.nth(5).inner_text())
        elif t.startswith("povprečno števi"):
            r["employees_2024"]=parse_number(c.nth(4).inner_text())
            r["employees_2025"]=parse_number(c.nth(5).inner_text())
    return r

def parse_company(page):
    txt=page.locator("body").inner_text()
    lines=[l.strip() for l in txt.splitlines() if l.strip()]
    def after(prefix):
        for l in lines:
            if l.startswith(prefix):
                return l.replace(prefix,"").strip()
        return ""
    addr=""
    municipality=""
    for i,l in enumerate(lines):
        if l.startswith("Matična številka:"):
            if i>0:
                addr=lines[i-1]
                m=re.search(r"(\d{4}\s+[A-Za-zČŠŽčšž\- ]+)$",addr)
                if m: municipality=m.group(1).strip()
            break
    d={
      "company_name":page.locator("h2.mL10").inner_text().strip(),
      "tax_number":after("Davčna številka:"),
      "registration_number":after("Matična številka:"),
      "address":addr,
      "activity":after("Dejavnost:"),
      "phone":after("Telefon:"),
      "website":after("URL:"),
      "email":after("E-pošta:"),
      "municipality": municipality
    }
    d.update(financial_values(page))
    return d

def main():
    initialize_database()
    with sync_playwright() as p:
        browser=p.chromium.connect_over_cdp(CDP_URL)
        page=find_gvin_page(browser.contexts[0])
        input("Press ENTER on results page...")
        links=get_all_company_links(page)
        print(f"Collected {len(links)} unique URLs")
        for i,h in enumerate(links,1):
            if i>1:
                human_pause(page)
            if i%100==0:
                page.wait_for_timeout(random.randint(30000,90000))
            print(f"[{i}/{len(links)}]")
            try:
                page.goto(BASE_URL+h)
                page.wait_for_load_state("networkidle")
                save_company(parse_company(page))
            except Exception as e:
                print(f"Skipped: {e}")
if __name__=="__main__":
    main()
