# Official source site learnings

Generated from live probes. Window: last 500 days. Per-source limit: 16.

This file records how official document sources behave for covered names, including endpoint access, naming/storage patterns, and what document families are actually reachable.

## tickers/STAN — Standard Chartered PLC

- Exchange adapter: `lse`
- IR page: `https://www.sc.com/en/investors/financial-results/`

### Endpoint checks

**Exchange / filing endpoints (London Stock Exchange / RNS)**

- `200` https://www.londonstockexchange.com/stock/STAN/standard-chartered-plc/company-page
- `200` https://www.rns-pdf.londonstockexchange.com

**Company website endpoints**

- `200` https://www.sc.com/en/investors/financial-results/

### Recorded learnings

- Standard Chartered stores archive files under `/uploads/sites/66/content/docs/` and `/uploads/sites/66/others/`, usually with filenames like `standard-chartered-plc-q3-2025-presentation.pdf` or `...-data-pack.xlsx`.
- Company page is a stable identity endpoint but much of the announcement data is JS-rendered.
- RNS PDFs are served from rns-pdf.londonstockexchange.com/rns/<id>.pdf.
- For Standard Chartered, the company IR archive is currently the more practical source for results, transcripts, presentations, and data packs.

### Observed document families

- `presentation`: 12
- `transcript`: 4

### Sample documents

- [presentation] undated | Download PDF
  - https://www.sc.com/en/uploads/sites/66/content/docs/standard-chartered-plc-full-year-2025-presentation.pdf
- [presentation] undated | standard-chartered-plc-q3-2025-presentation.pdf
  - https://www.sc.com/en/uploads/sites/66/content/docs/standard-chartered-plc-q3-2025-presentation.pdf
- [transcript] undated | standard-chartered-plc-q3-2025-results-transcript.pdf
  - https://www.sc.com/en/uploads/sites/66/content/docs/standard-chartered-plc-q3-2025-results-transcript.pdf
- [transcript] undated | standard-chartered-plc-full-year-2025-results-transcript.pdf
  - https://www.sc.com/en/uploads/sites/66/content/docs/standard-chartered-plc-full-year-2025-results-transcript.pdf
- [presentation] undated | 20260325-standard-chartered-plc-announcement-re-presentation-of-financial-information-rns.pdf
  - https://www.sc.com/en/uploads/sites/66/content/docs/20260325-standard-chartered-plc-announcement-re-presentation-of-financial-information-rns.pdf
- [presentation] undated | Q325-results-presentation-master-v3-Chinese.pdf
  - https://www.sc.com/en/uploads/sites/66/content/docs/Q325-results-presentation-master-v3-Chinese.pdf
- [presentation] undated | 20260325-re-presentation-of-financial-information-rns-CN.pdf
  - https://www.sc.com/en/uploads/sites/66/content/docs/20260325-re-presentation-of-financial-information-rns-CN.pdf
- [presentation] undated | 2003-Results-Presentation.pdf
  - https://www.sc.com/en/uploads/sites/66/content/docs/2003-Results-Presentation.pdf

### Periodic tracking implication

- Use the company IR archive as the primary periodic tracker; treat LSE/RNS as a supplementary announcement layer.

---

## tickers/HSBC — HSBC Holdings plc

- Exchange adapter: `hkex`
- IR page: `https://www.hsbc.com/investors/results-and-announcements`

### Endpoint checks

**Exchange / filing endpoints (HKEXnews)**

- `200` https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=en
- `404` https://www1.hkexnews.hk/listedco/listconews/sehk/

**Company website endpoints**

- `200` https://www.hsbc.com/investors/results-and-announcements
- `200` https://www.hsbc.com/investors/results-and-announcements/annual-report

### Recorded learnings

- HSBC stores result documents under `/-/files/hsbc/investors/hsbc-results/<year>/<period>/pdfs/hsbc-holdings-plc/...`.
- Official company code for this name is 0005.
- Search UI is public and accessible at search/titlesearch.xhtml.
- Announcement PDFs are commonly stored under www1.hkexnews.hk/listedco/listconews/sehk/YYYY/MMDD/<docid>.pdf.
- For deep dives, company IR sites often surface the same annual/interim materials in a more navigable archive than raw HKEX search results.

### Observed document families

- `annual_report`: 2
- `data_pack`: 14
- `presentation`: 6
- `results`: 2
- `transcript`: 8

### Sample documents

- [presentation] undated | Annual Results 2025 Presentation to Investors and Analysts
  - https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2025/annual/pdfs/hsbc-holdings-plc/260225-annual-results-2025-presentation-to-investors-and-analysts.pdf
- [data_pack] undated | 4Q 2025 Data Pack (Excel)
  - https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2025/annual/pdfs/hsbc-holdings-plc/260225-4q-2025-data-pack-excel.xlsx?sc_lang=en-gb
- [data_pack] undated | 4Q 2025 Data Pack (PDF)
  - https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2025/annual/pdfs/hsbc-holdings-plc/260225-4q-2025-data-pack.pdf
- [presentation] undated | Annual Results 2025 Fixed Income Investor Presentation
  - https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2025/annual/pdfs/hsbc-holdings-plc/260225-annual-results-2025-fixed-income-investor-presentation.pdf
- [annual_report] undated | Glossary: Annual Report and Accounts and Pillar 3 Disclosures 2025
  - https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2025/annual/pdfs/hsbc-holdings-plc/260225-glossary-annual-report-and-accounts-and-pillar-3-disclosures-2025.pdf
- [data_pack] undated | Capital and Other TLAC-eligible Instruments Main Features 31 December 2025 (Excel)
  - https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2025/annual/pdfs/hsbc-holdings-plc/260225-capital-and-other-tlac-eligible-instruments-main-features-31-december-2025-excel.xlsx?sc_lang=en-gb
- [data_pack] undated | ESG Datapack 2025 (Excel)
  - https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2025/annual/pdfs/hsbc-holdings-plc/260225-esg-datapack-2025-excel.xlsx?sc_lang=en-gb
- [data_pack] undated | ESG Datapack 2025 (PDF)
  - https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2025/annual/pdfs/hsbc-holdings-plc/260225-esg-datapack-2025.pdf

### Periodic tracking implication

- Use company IR archives for packaged results materials and HKEXnews as the official announcement backstop for filing-time validation.

---

## tickers/JPM — JPMorgan Chase & Co.

- Exchange adapter: `sec`
- IR page: `https://www.jpmorganchase.com/ir/quarterly-earnings`

### Endpoint checks

**Exchange / filing endpoints (SEC EDGAR)**

- `200` https://data.sec.gov/submissions/CIK0000019617.json
- `403` https://www.sec.gov/Archives/edgar/data/19617/

**Company website endpoints**

- `200` https://www.jpmorganchase.com/ir/quarterly-earnings
- `200` https://www.jpmorganchase.com/ir/annual-report

### Recorded learnings

- SEC files live under `/Archives/edgar/data/<cik_without_leading_zeroes>/<accession_without_dashes>/...`.
- Primary machine-readable endpoint is submissions/CIK##########.json.
- Document bodies live under sec.gov/Archives/edgar/data/<cik_without_leading_zeroes>/<accession_without_dashes>/.
- Best sources for deep dives are 10-K/10-Q and 8-K/6-K exhibits (press releases, earnings supplements, presentations).

### Observed document families

- `major_announcement`: 16
- `presentation`: 8
- `quarterly_report`: 8

### Sample documents

- [presentation] 2026-04-14 | 8-K a1q26_earningsxpresentat.htm
  - https://www.sec.gov/Archives/edgar/data/19617/000162828026025013/a1q26_earningsxpresentat.htm
- [quarterly_report] 2026-04-14 | 8-K a1q26erfex992supplement.htm
  - https://www.sec.gov/Archives/edgar/data/19617/000162828026024990/a1q26erfex992supplement.htm
- [major_announcement] 2026-02-23 | 8-K a2026companyupdatecoverp.htm
  - https://www.sec.gov/Archives/edgar/data/19617/000162828026010693/a2026companyupdatecoverp.htm
- [major_announcement] 2026-02-05 | 8-K d23804d8k.htm
  - https://www.sec.gov/Archives/edgar/data/19617/000119312526039427/d23804d8k.htm
- [major_announcement] 2026-01-22 | 8-K jpm-20260120.htm
  - https://www.sec.gov/Archives/edgar/data/19617/000001961726000052/jpm-20260120.htm
- [major_announcement] 2026-01-22 | 8-K d18152d8k.htm
  - https://www.sec.gov/Archives/edgar/data/19617/000119312526019450/d18152d8k.htm
- [presentation] 2026-01-13 | 8-K a4q25_earningsxpresentat.htm
  - https://www.sec.gov/Archives/edgar/data/19617/000162828026001915/a4q25_earningsxpresentat.htm
- [quarterly_report] 2026-01-13 | 8-K a4q25erfex992supplement.htm
  - https://www.sec.gov/Archives/edgar/data/19617/000162828026001902/a4q25erfex992supplement.htm

### Periodic tracking implication

- Track SEC cadence via submissions JSON and prioritize 10-K/10-Q plus 8-K exhibits for earnings and major events.

---

## tickers/1299 — AIA Group Limited

- Exchange adapter: `hkex`
- IR page: `https://www.aia.com/en/investor-relations/overview/results-presentations`

### Endpoint checks

**Exchange / filing endpoints (HKEXnews)**

- `200` https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=en
- `404` https://www1.hkexnews.hk/listedco/listconews/sehk/

**Company website endpoints**

- `200` https://www.aia.com/en/investor-relations/overview
- `200` https://www.aia.com/en/investor-relations/overview/results-presentations

### Recorded learnings

- AIA stores investor documents under `/content/dam/group-wise/en/docs/investor-relations/<year>/...`.
- Official company code for this name is 1299.
- Search UI is public and accessible at search/titlesearch.xhtml.
- Announcement PDFs are commonly stored under www1.hkexnews.hk/listedco/listconews/sehk/YYYY/MMDD/<docid>.pdf.
- For deep dives, company IR sites often surface the same annual/interim materials in a more navigable archive than raw HKEX search results.

### Observed document families

- `annual_report`: 4
- `data_pack`: 8
- `presentation`: 10
- `transcript`: 10

### Sample documents

- [annual_report] undated | Annual Report 2025
  - https://www.aia.com/content/dam/group-wise/en/docs/investor-relations/2026/2025%20Annual%20Report%20(Eng).pdf
- [presentation] undated | Presentation
  - https://www.aia.com/content/dam/group-wise/en/docs/investor-relations/2026/AIA%20Group%202025%20Annual%20Results%20Analyst%20Presentation%20EN%20(FINAL)%20-%2027%20Mar.pdf
- [data_pack] undated | Financial supplement
  - https://www.aia.com/content/dam/group-wise/en/docs/investor-relations/2026/FY%202025%20AIA%20Group%205-Year%20Financial%20Information_Final.xlsx
- [transcript] undated | Transcript (including Q&A session)
  - https://www.aia.com/content/dam/group-wise/en/docs/investor-relations/2026/AIA%20Group%20FY2025%20Analyst%20Presentation%20FINAL%20(Transcript%20with%20Q&A).pdf
- [presentation] undated | Presentation
  - https://www.aia.com/content/dam/group-wise/en/docs/investor-relations/2025/AIA%20Group%202025%20Interim%20Results%20Analyst%20Presentation%20(Final).pdf
- [data_pack] undated | Financial supplement
  - https://www.aia.com/content/dam/group-wise/en/docs/investor-relations/2025/1H%202025%20AIA%20Group%205-Year%20Financial%20Information_Final.xlsx
- [transcript] undated | Transcript (including Q&A session)
  - https://www.aia.com/content/dam/group-wise/en/docs/investor-relations/2025/AIA%20Group%201H2025%20Analyst%20Presentation%20FINAL%20(Transcript%20with%20Q&A).pdf
- [presentation] undated | Presentation
  - https://www.aia.com/content/dam/group-wise/en/docs/investor-relations/2025/AIA%20Group%202024%20Annual%20Results%20Analyst%20Presentation%20(Final).pdf

### Periodic tracking implication

- Use company IR archives for packaged results materials and HKEXnews as the official announcement backstop for filing-time validation.

---

## tickers/GOOG — Alphabet Inc.

- Exchange adapter: `sec`
- IR page: `https://abc.xyz/investor/`

### Endpoint checks

**Exchange / filing endpoints (SEC EDGAR)**

- `200` https://data.sec.gov/submissions/CIK0001652044.json
- `403` https://www.sec.gov/Archives/edgar/data/1652044/

**Company website endpoints**

- `200` https://abc.xyz/investor/
- `403` https://abc.xyz/investor/Earnings/default.aspx

### Recorded learnings

- SEC files live under `/Archives/edgar/data/<cik_without_leading_zeroes>/<accession_without_dashes>/...`.
- Alphabet's top-level IR homepage is accessible, but deeper results/event pages appear Cloudflare-protected (403) from this environment.
- Primary machine-readable endpoint is submissions/CIK##########.json.
- Document bodies live under sec.gov/Archives/edgar/data/<cik_without_leading_zeroes>/<accession_without_dashes>/.
- Best sources for deep dives are 10-K/10-Q and 8-K/6-K exhibits (press releases, earnings supplements, presentations).

### Observed document families

- `annual_report`: 2
- `major_announcement`: 20
- `quarterly_report`: 10

### Sample documents

- [major_announcement] 2026-04-10 | 8-K goog-20260407.htm
  - https://www.sec.gov/Archives/edgar/data/1652044/000165204426000034/goog-20260407.htm
- [major_announcement] 2026-04-02 | 8-K goog-20260330.htm
  - https://www.sec.gov/Archives/edgar/data/1652044/000165204426000031/goog-20260330.htm
- [major_announcement] 2026-03-06 | 8-K goog-20260304.htm
  - https://www.sec.gov/Archives/edgar/data/1652044/000165204426000026/goog-20260304.htm
- [major_announcement] 2026-02-13 | 8-K d946885d8k.htm
  - https://www.sec.gov/Archives/edgar/data/1652044/000119312526051423/d946885d8k.htm
- [annual_report] 2026-02-05 | 10-K goog-20251231.htm
  - https://www.sec.gov/Archives/edgar/data/1652044/000165204426000018/goog-20251231.htm
- [quarterly_report] 2026-02-04 | 8-K googexhibit991q42025.htm
  - https://www.sec.gov/Archives/edgar/data/1652044/000165204426000012/googexhibit991q42025.htm
- [major_announcement] 2025-11-06 | 8-K d56115d8k.htm
  - https://www.sec.gov/Archives/edgar/data/1652044/000119312525269979/d56115d8k.htm
- [quarterly_report] 2025-10-30 | 10-Q goog-20250930.htm
  - https://www.sec.gov/Archives/edgar/data/1652044/000165204425000091/goog-20250930.htm

### Periodic tracking implication

- Track SEC cadence via submissions JSON and prioritize 10-K/10-Q plus 8-K exhibits for earnings and major events.

---
