# OfferScraper

Location-aware product offer scraper.

## Inputs

- `product_keyword` (required): category or search term like `shoes`
- `country` (required): must be `India` (India-only support)
- `pincode` (required): postal code for delivery filtering

## Output fields

For each selected best offer (one per brand):

- `brand_name`
- `product_title`
- `best_available_price`
- `original_price` (if available)
- `source_website`
- `source_url`
- `delivery_availability` (`available`, `unavailable`, or `unknown`)

## Run

```bash
python3 -m pip install -r requirements.txt
python3 scraper.py "shoes" "India" "560001" --pretty
```

## Notes

- Uses multiple providers (`nykaa.com`, `nykaaman.com`, `adidas.co.in`, `in.puma.com`, `reebok.in`, `ebay.in`, `dummyjson.com`) for product discovery.
- Handles pagination in each provider.
- Applies retry + backoff for temporary rate-limit/server errors.
- Filters out unavailable delivery entries and keeps only the lowest price per brand.
