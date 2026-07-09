# pyright: reportOptionalMemberAccess=false, reportOptionalSubscript=false, reportArgumentType=false, reportCallIssue=false, reportAttributeAccessIssue=false
# ruff: noqa: F403,F405
from smoke_support import *


class MsrpSmokeTests(SmokeBaseTest):
    def test_msrp_scraper_reads_full_retail_fallback(self):
        html = '<html><body><script>{"fullRetail":184.0,"actualPrice":184.0}</script></body></html>'

        with mock.patch("scraping.requests.get") as mocked_get:
            mocked_get.return_value.status_code = 200
            mocked_get.return_value.url = "https://www.cutco.com/p/1766C"
            mocked_get.return_value.text = html

            self.assertEqual(
                _scrape_price_from_page("https://www.cutco.com/p/1766C"), 184.0
            )

    def test_msrp_scraper_ignores_zero_price_noise(self):
        html = """
            <html>
              <body>
                <div class="promo">$0.00</div>
                <h1>#124 Small Cutting Board</h1>
                <div class="price">$35</div>
              </body>
            </html>
        """

        with mock.patch("scraping.requests.get") as mocked_get:
            mocked_get.return_value.status_code = 200
            mocked_get.return_value.url = "https://www.cutco.com/p/small-cutting-board"
            mocked_get.return_value.text = html

            self.assertEqual(
                _scrape_price_from_page("https://www.cutco.com/p/small-cutting-board"),
                35.0,
            )

    def test_msrp_scraper_ignores_related_item_prices_after_purchase_area(self):
        html = """
            <html>
              <body>
                <h1>#125 Medium Cutting Board</h1>
                <div class="price">$38</div>
                <button>Add to Cart</button>
                <section>
                  <h2>Frequently Bought Together</h2>
                  <p>Large Cutting Board $42</p>
                  <p>Super Shears $149</p>
                  <p>Bundle Price $481</p>
                </section>
              </body>
            </html>
        """

        with mock.patch("scraping.requests.get") as mocked_get:
            mocked_get.return_value.status_code = 200
            mocked_get.return_value.url = "https://www.cutco.com/p/medium-cutting-board"
            mocked_get.return_value.text = html

            self.assertEqual(
                _scrape_price_from_page("https://www.cutco.com/p/medium-cutting-board"),
                38.0,
            )

    def test_find_stale_msrp_rows_flags_zero_and_missing_prices(self):
        with self.app.app_context():
            zero_item = Item(
                name="Zero Knife", sku="Z-1", category="Kitchen Knives", msrp=0.0
            )
            missing_item = Item(
                name="Missing Knife", sku="M-1", category="Kitchen Knives", msrp=None
            )
            priced_item = Item(
                name="Priced Knife", sku="P-1", category="Kitchen Knives", msrp=18.0
            )
            db.session.add_all([zero_item, missing_item, priced_item])
            db.session.commit()

            rows = find_stale_msrp_rows(Item.query.all())

        self.assertEqual([row["sku"] for row in rows], ["M-1", "Z-1"])

    def test_msrp_scraper_maps_cutting_board_family_urls_to_specific_products(self):
        cases = [
            (
                "124",
                "https://www.cutco.com/p/cutting-boards/124",
                "https://www.cutco.com/p/small-cutting-board",
                35.0,
            ),
            (
                "125",
                "https://www.cutco.com/p/cutting-boards/125",
                "https://www.cutco.com/p/medium-cutting-board",
                28.0,
            ),
            (
                "126",
                "https://www.cutco.com/p/cutting-boards/126",
                "https://www.cutco.com/p/large-cutting-board",
                42.0,
            ),
        ]

        for sku, family_url, product_url, expected_price in cases:
            with self.subTest(family_url=family_url):
                html = f"""
                    <html>
                      <body>
                        <h1>{product_url.rsplit("/", 1)[-1].replace("-", " ").title()}</h1>
                        <div class="price">${expected_price:.2f}</div>
                        <script>
                          window.__CUTCO__ = {{"fullRetail":198.00,"actualPrice":198.00}};
                        </script>
                      </body>
                    </html>
                """

                response = mock.Mock(status_code=200, url=product_url, text=html)
                with mock.patch(
                    "scraping.requests.get", return_value=response
                ) as mocked_get:
                    self.assertEqual(
                        _scrape_price_from_page(family_url, sku=sku), expected_price
                    )
                    mocked_get.assert_called_once()
                    self.assertEqual(mocked_get.call_args.args[0], product_url)

    def test_msrp_scraper_rejects_family_pages_without_exact_product_url(self):
        html = """
            <html>
              <body>
                <h1>Flatware</h1>
                <section>
                  <a href="/p/stainless-place-setting">
                    <h2>Cutco 5-Pc. Stainless Place Setting with Stainless Table Knife</h2>
                    <span class="price">$211</span>
                  </a>
                  <a href="/p/stainless-dinner-fork">
                    <h2>Cutco Stainless Dinner Fork</h2>
                    <span class="price">$39</span>
                  </a>
                </section>
              </body>
            </html>
        """

        response = mock.Mock(
            status_code=200, url="https://www.cutco.com/shop/flatware", text=html
        )
        with mock.patch("scraping.requests.get", return_value=response):
            self.assertIsNone(
                _scrape_price_from_page(
                    "https://www.cutco.com/shop/flatware", "Stainless Dinner Fork"
                )
            )

    def test_msrp_scraper_rejects_family_page_item_hop_guessing(self):
        family_html = """
            <html>
              <body>
                <a href="/p/stainless-place-setting"><h2>Cutco 5-Pc. Stainless Place Setting with Stainless Table Knife</h2></a>
                <a href="/p/stainless-dinner-fork"><h2>Cutco Stainless Dinner Fork</h2></a>
              </body>
            </html>
        """

        with mock.patch("scraping.requests.get") as mocked_get:
            mocked_get.return_value = mock.Mock(
                status_code=200,
                url="https://www.cutco.com/shop/flatware",
                text=family_html,
            )

            self.assertIsNone(
                _scrape_price_from_page(
                    "https://www.cutco.com/shop/flatware", "Stainless Dinner Fork"
                ),
            )
            self.assertEqual(
                mocked_get.call_args_list[0].args[0],
                "https://www.cutco.com/shop/flatware",
            )

    def test_resolve_cutco_item_page_url_hops_to_matching_item_page(self):
        family_html = """
            <html>
              <body>
                <a href="/p/stainless-place-setting"><h2>Cutco 5-Pc. Stainless Place Setting with Stainless Table Knife</h2></a>
                <a href="/p/stainless-dinner-fork"><h2>Cutco Stainless Dinner Fork</h2></a>
              </body>
            </html>
        """
        item_html = """
            <html>
              <body>
                <h1>#1950 Stainless Dinner Fork</h1>
                <div class="price">$39</div>
              </body>
            </html>
        """

        with mock.patch("scraping.requests.get") as mocked_get:
            mocked_get.side_effect = [
                mock.Mock(
                    status_code=200,
                    url="https://www.cutco.com/shop/flatware",
                    text=family_html,
                ),
                mock.Mock(
                    status_code=200,
                    url="https://www.cutco.com/p/stainless-dinner-fork",
                    text=item_html,
                ),
            ]

            self.assertEqual(
                _resolve_cutco_item_page_url(
                    "https://www.cutco.com/shop/flatware",
                    item_name="Stainless Dinner Fork",
                ),
                "https://www.cutco.com/p/stainless-dinner-fork",
            )

    def test_msrp_scraper_prefers_page_js_price_over_json_ld_offer(self):
        html = """
            <html>
              <head>
                <script type="application/ld+json">
                  {"@type":"Product","sku":"677CD","offers":{"price":159.00}}
                </script>
              </head>
              <body>
                <script>
                  window.__CUTCO__ = {"fullRetail":149.00,"actualPrice":149.00};
                </script>
              </body>
            </html>
        """

        with mock.patch("scraping.requests.get") as mocked_get:
            mocked_get.side_effect = [
                mock.Mock(
                    status_code=200,
                    url="https://www.cutco.com/p/super-shears/677CD",
                    text=html,
                ),
            ]

            self.assertEqual(
                _scrape_price_from_page("https://www.cutco.com/p/super-shears/677CD"),
                149.0,
            )

        with mock.patch("scraping.requests.get") as mocked_get:
            mocked_get.return_value = mock.Mock(
                status_code=200,
                url="https://www.cutco.com/p/super-shears/677CD",
                text=html,
            )

            self.assertEqual(
                scrape_item_specs("https://www.cutco.com/p/super-shears/677CD")["msrp"],
                149.0,
            )

    def test_msrp_scraper_ignores_sheath_bundle_price_noise(self):
        html = """
            <html>
              <body>
                <h1>Super Shears</h1>
                <div class="price">$149</div>
                <div>Super Shears with Sheath</div>
                <div class="price">$198</div>
              </body>
            </html>
        """

        with mock.patch("scraping.requests.get") as mocked_get:
            mocked_get.return_value = mock.Mock(
                status_code=200,
                url="https://www.cutco.com/p/super-shears/77CD",
                text=html,
            )

            self.assertEqual(
                _scrape_price_from_page(
                    "https://www.cutco.com/p/super-shears/77CD", "Super Shears"
                ),
                149.0,
            )

    def test_msrp_scraper_prefers_exact_item_title_over_variant_substring(self):
        html = """
            <html>
              <body>
                <h1>#1759C Table Knife</h1>
                <div class="price">$54</div>
                <div>Also available as Stainless Table Knife</div>
                <div class="price">$78</div>
              </body>
            </html>
        """

        with mock.patch("scraping.requests.get") as mocked_get:
            mocked_get.return_value = mock.Mock(
                status_code=200,
                url="https://www.cutco.com/p/1759C",
                text=html,
            )

            self.assertEqual(
                _scrape_price_from_page("https://www.cutco.com/p/1759C", "Table Knife"),
                54.0,
            )

    def test_msrp_scraper_ignores_late_related_item_price_on_exact_page(self):
        html = """
            <html>
              <body>
                <h1>#1504 Cheese Knife</h1>
                <div class="price">$98</div>
                <div>Regular shipping included</div>
                <section>
                  <h2>Frequently Bought Together</h2>
                  <p>Super Shears $149</p>
                  <p>Bundle Price $481</p>
                </section>
              </body>
            </html>
        """

        with mock.patch("scraping.requests.get") as mocked_get:
            mocked_get.return_value = mock.Mock(
                status_code=200,
                url="https://www.cutco.com/p/cheese-knife",
                text=html,
            )

            self.assertEqual(
                _scrape_price_from_page(
                    "https://www.cutco.com/p/cheese-knife", "Cheese Knife"
                ),
                98.0,
            )

    def test_msrp_scraper_anchors_on_sku_when_title_has_extra_descriptor(self):
        html = """
            <html>
              <body>
                <h1>#1501 Vegetable & Potato Peeler</h1>
                <div class="price">$55</div>
                <div>Regular shipping included</div>
                <section>
                  <h2>Frequently Bought Together</h2>
                  <p>Kitchen Tool Holder $149</p>
                </section>
              </body>
            </html>
        """

        self.assertEqual(
            _extract_cutco_price(
                html,
                page_url="https://www.cutco.com/p/vegetable-peeler",
                item_name="Vegetable Peeler",
                sku="1501",
            ),
            55.0,
        )

    def test_find_cutco_item_link_prefers_exact_item_over_sheath_bundle(self):
        html = """
            <html>
              <body>
                <a href="/p/super-shears-with-sheath"><h2>Super Shears with Sheath</h2></a>
                <a href="/p/super-shears"><h2>Super Shears</h2></a>
              </body>
            </html>
        """

        self.assertEqual(
            _find_cutco_item_link(html, "Super Shears"),
            "https://www.cutco.com/p/super-shears",
        )
        self.assertEqual(
            _find_cutco_item_link(html, "Super Shears with Sheath"),
            "https://www.cutco.com/p/super-shears-with-sheath",
        )
