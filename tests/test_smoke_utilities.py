# pyright: reportOptionalMemberAccess=false, reportOptionalSubscript=false, reportArgumentType=false, reportCallIssue=false, reportAttributeAccessIssue=false
# ruff: noqa: F403,F405
from smoke_support import *


class UtilitySmokeTests(SmokeBaseTest):
    def test_token_helpers_validate_and_reject_tampering(self):
        self._login_as_admin()
        self._set_csrf_token()

        with self.app.app_context():
            gift_token = _gift_token(12, 34)
            collection_token = _collection_token(56)
            self.assertEqual(_verify_gift_token(gift_token), (12, 34))
            self.assertEqual(_verify_collection_token(collection_token), 56)
            self.assertIsNone(_verify_gift_token("not-a-token"))
            self.assertIsNone(_verify_collection_token("not-a-token"))
            self.assertIsNone(_verify_gift_token(gift_token + "x"))
            self.assertIsNone(_verify_collection_token(collection_token + "x"))

    def test_notify_discord_handles_success_and_failure(self):
        with mock.patch("helpers.DISCORD_WEBHOOK_URL", None):
            self.assertFalse(_notify_discord("No webhook"))

        response = mock.Mock()
        response.raise_for_status.return_value = None
        with (
            mock.patch("helpers.DISCORD_WEBHOOK_URL", "https://discord.invalid"),
            mock.patch("helpers.requests.post", return_value=response) as post_mock,
        ):
            self.assertTrue(_notify_discord("Webhook works"))
            post_mock.assert_called_once()

        with (
            mock.patch("helpers.DISCORD_WEBHOOK_URL", "https://discord.invalid"),
            mock.patch("helpers.requests.post", side_effect=RuntimeError("boom")),
        ):
            self.assertFalse(_notify_discord("Webhook fails"))

    def test_set_member_entries_preserve_structured_skus(self):
        structured_members = [
            {"sku": "BBQ-1", "name": "Barbecue Tongs", "quantity": 1},
            {"sku": "BBQ-2", "name": "Barbecue Turner", "quantity": 2},
        ]
        visible_rows = [
            {"name": "Barbecue Tongs", "is_set_only": False},
            {"name": "Barbecue Turner", "is_set_only": False},
            {"name": "Extra Piece", "is_set_only": True},
        ]

        member_entries = _build_set_member_entries(
            structured_members,
            visible_rows,
            ["BBQ-1", "BBQ-2", "BBQ-3"],
            {"BBQ-1": 1, "BBQ-2": 2, "BBQ-3": 1},
        )

        self.assertEqual(member_entries[0]["sku"], "BBQ-1")
        self.assertEqual(member_entries[0]["name"], "Barbecue Tongs")
        self.assertEqual(member_entries[1]["sku"], "BBQ-2")
        self.assertEqual(member_entries[1]["quantity"], 2)
        self.assertEqual(member_entries[2]["sku"], "BBQ-3")
        self.assertEqual(member_entries[2]["name"], "Extra Piece")
        self.assertTrue(member_entries[2]["is_set_only"])

    def test_set_member_entries_prefer_visible_individual_skus_over_set_sku(self):
        structured_members = [
            {"sku": "1820", "name": "Salad Tongs", "quantity": 1},
            {"sku": "1820", "name": "Salad Fork", "quantity": 1},
        ]
        visible_rows = [
            {"name": "Salad Tongs", "sku": "1708", "is_set_only": False},
            {"name": "Salad Fork", "sku": "1707", "is_set_only": False},
        ]

        member_entries = _build_set_member_entries(
            structured_members,
            visible_rows,
            ["1820", "1820"],
            {"1820": 1},
        )

        self.assertEqual([entry["sku"] for entry in member_entries], ["1708", "1707"])
        self.assertEqual(
            [entry["name"] for entry in member_entries], ["Salad Tongs", "Salad Fork"]
        )

    def test_load_member_snapshot_dedupes_duplicate_skus(self):
        rows = _load_member_snapshot(
            json.dumps(
                [
                    {"sku": "10", "name": "Knife One", "quantity": 1},
                    {"sku": "10", "name": "Knife One", "quantity": 11},
                    {"sku": "1741", "name": "Knife Two", "quantity": 1},
                ]
            )
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["sku"], "10")
        self.assertEqual(rows[0]["quantity"], 12)
        self.assertEqual(rows[1]["sku"], "1741")

    def test_normalize_set_member_skus_strips_variant_suffixes(self):
        self.assertEqual(_normalize_set_member_sku("1737W-1"), "1737")
        self.assertEqual(_normalize_set_member_sku("1737C-1"), "1737")
        self.assertEqual(_normalize_set_member_sku("1737/1"), "1737")
        self.assertEqual(_normalize_set_member_sku("77-"), "77")
        self.assertEqual(_normalize_set_member_sku("990C"), "990C")
        self.assertEqual(_normalize_set_member_sku("2120-2"), "2120-2")
        self.assertEqual(_normalize_set_member_sku("2130CD"), "2130CD")
        self.assertEqual(_normalize_set_member_sku("3721CSH"), "3721CSH")
        self.assertEqual(_normalize_set_member_sku("2026D"), "2026D")
        self.assertEqual(_normalize_set_member_sku("1716C"), "1716")
        self.assertEqual(_normalize_set_member_sku("1737"), "1737")
        self.assertIsNone(_normalize_set_member_sku(""))

    def test_extract_sku_from_href_can_preserve_lettered_code(self):
        self.assertEqual(_extract_sku_from_href("https://www.cutco.com/p/990c"), "990")
        self.assertEqual(
            _extract_sku_from_href(
                "https://www.cutco.com/p/990c", preserve_lettered_code=True
            ),
            "990C",
        )
        self.assertEqual(
            _extract_sku_from_href(
                "https://www.cutco.com/p/4135-2", preserve_lettered_code=True
            ),
            "4135-2",
        )
        self.assertEqual(
            _extract_sku_from_href(
                "https://www.cutco.com/p/2135-2", preserve_lettered_code=True
            ),
            "2135-2",
        )
        self.assertIsNone(
            _extract_sku_from_href(
                "https://www.cutco.com/p/250th-celebration-knife-with-sheath&view=product"
            )
        )

    def test_fetch_sku_from_page_ignores_descriptive_slug_digits(self):
        response = mock.Mock()
        response.status_code = 200
        response.text = """
            <html>
              <head>
                <meta property="product:sku" content="2135">
              </head>
              <body>
                <h1>The 250th Celebration Knife with Sheath</h1>
              </body>
            </html>
        """
        with mock.patch("scraping.requests.get", return_value=response):
            from scraping import _fetch_sku_from_page

            _fetch_sku_from_page.cache_clear()
            sku, name = _fetch_sku_from_page(
                "https://www.cutco.com/p/250th-celebration-knife-with-sheath&view=product"
            )
        self.assertEqual(sku, "2135")
        self.assertEqual(name, "The 250th Celebration Knife with Sheath")

    def test_extract_product_variant_colors_uses_page_color_without_swatch_group(self):
        response = mock.Mock()
        response.status_code = 200
        response.text = """
            <html><body>
              <div>Color: Classic</div>
              <button>Select Classic Image: Classic</button>
              <button>Select Pearl Image: Pearl</button>
              <button>Select Red Image: Red</button>
            </body></html>
        """
        with mock.patch("scraping.requests.get", return_value=response):
            _extract_product_variant_colors.cache_clear()
            self.assertEqual(
                _extract_product_variant_colors("https://www.cutco.com/p/1738C-test-1"),
                ("Classic",),
            )

    def test_extract_product_variant_colors_ignores_attribute_sources_without_swatch_group(
        self,
    ):
        response = mock.Mock()
        response.status_code = 200
        response.text = """
            <html><body>
              <button aria-label="Select Classic Image: Classic">Classic</button>
              <div data-color="Pearl">Pearl</div>
              <option value="Red">Red</option>
            </body></html>
        """
        with mock.patch("scraping.requests.get", return_value=response):
            _extract_product_variant_colors.cache_clear()
            self.assertEqual(
                _extract_product_variant_colors("https://www.cutco.com/p/1738C-test-2"),
                (),
            )

    def test_extract_product_variant_colors_parses_color_swatches(self):
        response = mock.Mock()
        response.status_code = 200
        response.text = """
            <html><body>
              <fieldset class="swatch-group Color" data-type="Tools Handle Color">
                <div class="swatch product-option color-swatch" data-option="Classic">
                  <label for="swatch-Classic">
                    <span class="reader-only">Select Classic</span>
                  </label>
                </div>
                <div class="swatch product-option color-swatch" data-option="Pearl">
                  <label for="swatch-Pearl">
                    <span class="reader-only">Select Pearl</span>
                  </label>
                </div>
                <div class="swatch product-option color-swatch" data-option="Red">
                  <label for="swatch-Red">
                    <span class="reader-only">Select Red</span>
                  </label>
                </div>
              </fieldset>
            </body></html>
        """
        with mock.patch("scraping.requests.get", return_value=response):
            self.assertEqual(
                _extract_product_variant_colors("https://www.cutco.com/p/1726C-test-1"),
                ("Classic", "Pearl", "Red"),
            )

    def test_extract_product_variant_colors_keeps_compound_swatch_labels_together(self):
        response = mock.Mock()
        response.status_code = 200
        response.text = """
            <html><body>
              <fieldset class="swatch-group Color" data-type="Tools Handle Color">
                <div class="swatch product-option color-swatch" data-option="Classic Brown" data-code="Brown">
                  <label for="swatch-Classic-Brown">
                    <span class="reader-only">Select Classic Brown</span>
                  </label>
                </div>
                <div class="swatch product-option color-swatch" data-option="Pearl">
                  <label for="swatch-Pearl">
                    <span class="reader-only">Select Pearl</span>
                  </label>
                </div>
              </fieldset>
            </body></html>
        """
        with mock.patch("scraping.requests.get", return_value=response):
            _extract_product_variant_colors.cache_clear()
            self.assertEqual(
                _extract_product_variant_colors("https://www.cutco.com/p/1726C-test-2"),
                ("Classic Brown", "Pearl"),
            )

    def test_extract_product_variant_colors_ignores_page_copy_when_swatches_exist(self):
        response = mock.Mock()
        response.status_code = 200
        response.text = """
            <html><body>
              <p>All Cutco Knives Are American Made</p>
              <fieldset class="swatch-group Color" data-type="Tools Handle Color">
                <div class="swatch product-option color-swatch" data-option="Classic Brown">
                  <label for="swatch-Classic-Brown">
                    <span class="reader-only">Select Classic Brown</span>
                  </label>
                </div>
                <div class="swatch product-option color-swatch" data-option="Pearl">
                  <label for="swatch-Pearl">
                    <span class="reader-only">Select Pearl</span>
                  </label>
                </div>
              </fieldset>
            </body></html>
        """
        with mock.patch("scraping.requests.get", return_value=response):
            _extract_product_variant_colors.cache_clear()
            self.assertEqual(
                _extract_product_variant_colors("https://www.cutco.com/p/9999-test"),
                ("Classic Brown", "Pearl"),
            )

    def test_extract_product_variant_colors_prefers_color_swatches_over_generic_option_copy(
        self,
    ):
        response = mock.Mock()
        response.status_code = 200
        response.text = """
            <html><body>
              <fieldset class="swatch-group Color" data-type="Tools Handle Color">
                <div class="swatch product-option color-swatch" data-option="Gray">
                  <label for="swatch-Gray">
                    <span class="reader-only">Select Gray</span>
                  </label>
                </div>
                <div class="swatch product-option" data-option="6\" Vegetable Knife With Sheath">
                  <label for="swatch-Junk">
                    <span class="reader-only">Select 6&quot; Vegetable Knife With Sheath</span>
                  </label>
                </div>
                <div class="swatch product-option" data-option="All Cutco Knives Are American Made">
                  <label for="swatch-Noise">
                    <span class="reader-only">Select All Cutco Knives Are American Made</span>
                  </label>
                </div>
              </fieldset>
            </body></html>
        """
        with mock.patch("scraping.requests.get", return_value=response):
            _extract_product_variant_colors.cache_clear()
            self.assertEqual(
                _extract_product_variant_colors("https://www.cutco.com/p/9998-test"),
                ("Gray",),
            )

    def test_extract_product_variant_colors_keeps_generic_color_labels_when_no_color_swatch_class_exists(
        self,
    ):
        response = mock.Mock()
        response.status_code = 200
        response.text = """
            <html><body>
              <fieldset class="swatch-group Color" data-type="Tools Handle Color">
                <div class="swatch product-option" data-option="Classic Brown">
                  <label for="swatch-Classic-Brown">
                    <span class="reader-only">Select Classic Brown</span>
                  </label>
                </div>
                <div class="swatch product-option" data-option="Pearl">
                  <label for="swatch-Pearl">
                    <span class="reader-only">Select Pearl</span>
                  </label>
                </div>
              </fieldset>
            </body></html>
        """
        with mock.patch("scraping.requests.get", return_value=response):
            _extract_product_variant_colors.cache_clear()
            self.assertEqual(
                _extract_product_variant_colors("https://www.cutco.com/p/9997-test"),
                ("Classic Brown", "Pearl"),
            )

    def test_extract_product_variant_colors_rejects_non_color_labels_in_generic_swatch_groups(
        self,
    ):
        response = mock.Mock()
        response.status_code = 200
        response.text = """
            <html><body>
              <fieldset class="swatch-group Color" data-type="Tools Handle Color">
                <div class="swatch product-option" data-option="Gray">
                  <label for="swatch-Gray">
                    <span class="reader-only">Select Gray</span>
                  </label>
                </div>
                <div class="swatch product-option" data-option="Chat Live">
                  <label for="swatch-Chat">
                    <span class="reader-only">Select Chat Live</span>
                  </label>
                </div>
                <div class="swatch product-option" data-option="Customer Service">
                  <label for="swatch-Service">
                    <span class="reader-only">Select Customer Service</span>
                  </label>
                </div>
                <div class="swatch product-option" data-option="All Cutco Knives Are American Made">
                  <label for="swatch-Noise">
                    <span class="reader-only">Select All Cutco Knives Are American Made</span>
                  </label>
                </div>
              </fieldset>
            </body></html>
        """
        with mock.patch("scraping.requests.get", return_value=response):
            _extract_product_variant_colors.cache_clear()
            self.assertEqual(
                _extract_product_variant_colors("https://www.cutco.com/p/9996-test"),
                ("Gray",),
            )

    def test_extract_product_variant_colors_uses_page_color_when_no_swatch_group_exists(
        self,
    ):
        response = mock.Mock()
        response.status_code = 200
        response.text = """
            <html><body>
              <h1>Handle Mitt</h1>
              <p>All Cutco Knives Are American Made</p>
              <p>Exclusive for Cutco</p>
              <p>Customer Service</p>
              <p>Chat Live</p>
              <p>Color: Classic</p>
              <p>Select Classic Image: Classic</p>
              <p>Select Pearl Image: Pearl</p>
              <p>Select Red Image: Red</p>
            </body></html>
        """
        with mock.patch("scraping.requests.get", return_value=response):
            _extract_product_variant_colors.cache_clear()
            self.assertEqual(
                _extract_product_variant_colors("https://www.cutco.com/p/278-test"),
                ("Classic",),
            )

    def test_extract_product_variant_colors_detects_purple_campaign_pages(self):
        response = mock.Mock()
        response.status_code = 200
        response.text = """
            <html><body>
              <h1>Cutco Cares 2026 - Alzheimer's Association</h1>
              <p>Purple Products:</p>
              <input type="radio" name="purple_products" value="Super Shears"
                     data-type="Purple Products" data-code="77L">
              <input type="radio" name="purple_products" value="Cutting Board"
                     data-type="Purple Products" data-code="125L">
              <input type="radio" name="purple_products" value="Santoku-Style Trimmer"
                     data-type="Purple Products" data-code="3721LSH">
            </body></html>
        """
        with mock.patch("scraping.requests.get", return_value=response):
            _extract_product_variant_colors.cache_clear()
            self.assertEqual(
                _extract_product_variant_colors(
                    "https://www.cutco.com/p/cutco-cares-alzheimers/"
                ),
                ("Purple",),
            )

    def test_scrape_purple_campaign_variants_includes_sheathed_promo_items(self):
        response = mock.Mock()
        response.status_code = 200
        response.text = """
            <html><body>
              <input type="radio" name="purple_products" value="Purple Super Shears"
                     data-type="Purple Products" data-code="77L">
            </body></html>
        """
        with mock.patch("scraping.requests.get", return_value=response):
            scrape_purple_campaign_variants.cache_clear()
            entries = scrape_purple_campaign_variants()
        entry_names = {entry["name"] for entry in entries}
        self.assertIn('Purple 7" Santoku with Sheath', entry_names)
        self.assertIn("Purple Santoku-Style Trimmer with Sheath", entry_names)
        self.assertNotIn("Purple Traditional Cheese Knife with Sheath", entry_names)
        self.assertNotIn('Purple 5" Petite Santoku with Sheath', entry_names)

    def test_extract_product_variant_colors_prefers_selected_color_on_size_pages(self):
        response = mock.Mock()
        response.status_code = 200
        response.text = """
            <html><body>
              <fieldset class="swatch-group Color" data-type="Color">
                <div class="swatch product-option color-swatch" data-option="Gray">
                  <label for="swatch-Gray">
                    <span class="reader-only">Select Gray</span>
                  </label>
                </div>
                <div class="swatch product-option color-swatch" data-option="Red">
                  <label for="swatch-Red">
                    <span class="reader-only">Select Red</span>
                  </label>
                </div>
              </fieldset>
              <fieldset class="swatch-group Size" data-type="Size">
                <div class="swatch product-option" data-option="S (8&quot; × 12&quot;)">
                  <label for="swatch-Size-S">
                    <span class="reader-only">S (8&quot; × 12&quot;)</span>
                  </label>
                </div>
              </fieldset>
              <p>Color: Gray</p>
            </body></html>
        """
        with mock.patch("scraping.requests.get", return_value=response):
            _extract_product_variant_colors.cache_clear()
            self.assertEqual(
                _extract_product_variant_colors(
                    "https://www.cutco.com/p/cutting-boards"
                ),
                ("Gray",),
            )

    def test_extract_product_variant_colors_parses_color_dropdowns(self):
        response = mock.Mock()
        response.status_code = 200
        response.text = """
            <html><body>
              <label for="finish">Finish</label>
              <select id="finish" name="finish">
                <option value="">Choose a finish</option>
                <option value="Stainless">Stainless</option>
                <option value="Pearl">Pearl</option>
                <option value="Black">Black</option>
              </select>
            </body></html>
        """
        with mock.patch("scraping.requests.get", return_value=response):
            _extract_product_variant_colors.cache_clear()
            self.assertEqual(
                _extract_product_variant_colors("https://www.cutco.com/p/1570W-test"),
                ("Stainless", "Pearl", "Black"),
            )

    def test_extract_product_variant_colors_uses_web_item_options_without_item_names(
        self,
    ):
        response = mock.Mock()
        response.status_code = 200
        response.text = """
            <html><body><script>
              const webItemsMap = {
                "1570W": {
                  "itemName": "6-Pc. Traditional Accessory Set",
                  "displayedOptions": [{
                    "optionType": "Flatware Handle Color",
                    "displayedType": "Color",
                    "optionCode": "Pearl",
                    "description": "Pearl"
                  }],
                  "itemSetList": [{"name": "Traditional Gravy Ladle"}]
                },
                "1570C": {
                  "itemName": "6-Pc. Traditional Accessory Set",
                  "displayedOptions": [{
                    "optionType": "Flatware Handle Color",
                    "displayedType": "Color",
                    "optionCode": "Classic",
                    "description": "Classic"
                  }],
                  "itemSetList": [{"name": "Traditional Serving Spoon"}]
                }
              };
            </script></body></html>
        """
        with mock.patch("scraping.requests.get", return_value=response):
            _extract_product_variant_colors.cache_clear()
            self.assertEqual(
                _extract_product_variant_colors(
                    "https://www.cutco.com/p/traditional-flatware-accessories/1570W"
                ),
                ("Pearl", "Classic"),
            )

    def test_set_variant_options_reject_product_names_and_member_only_colors(self):
        response = mock.Mock()
        response.status_code = 200
        response.text = """
            <html><body>
              <fieldset class="swatch-group Color" data-type="Handle Color">
                <div class="swatch product-option color-swatch" data-option="Classic"></div>
                <div class="swatch product-option color-swatch" data-option="Pearl"></div>
                <div class="swatch product-option color-swatch" data-option="Red"></div>
              </fieldset>
              <script>
                const webItemsMap = {
                  "3822CD": {"displayedOptions": [{"optionType": "Color", "description": "Santoku-Style"}]},
                  "125": {"displayedOptions": [{"optionType": "Color", "description": "Red"}]},
                  "1905": {"itemName": "3-Pc. Cutting Board Set"}
                };
              </script>
            </body></html>
        """
        board_response = mock.Mock()
        board_response.status_code = 200
        board_response.text = """
            <html><body><script>
              const webItemsMap = {
                "125": {"displayedOptions": [{"optionType": "Color", "description": "Red"}]},
                "1905": {"itemName": "3-Pc. Cutting Board Set"}
              };
            </script></body></html>
        """
        with mock.patch(
            "scraping.requests.get",
            side_effect=lambda url, **_kwargs: (
                board_response if "cutting-board-set" in url else response
            ),
        ):
            scrape_set_variant_options.cache_clear()
            self.assertEqual(
                scrape_set_variant_options("https://www.cutco.com/p/3822CD", "3822"),
                {
                    "handle_colors": ("Classic", "Pearl", "Red"),
                    "block_finishes": (),
                },
            )
            scrape_set_variant_options.cache_clear()
            self.assertEqual(
                scrape_set_variant_options(
                    "https://www.cutco.com/p/3-pc-cutting-board-set", "1905"
                ),
                {"handle_colors": (), "block_finishes": ()},
            )

    def test_set_variant_options_separate_handles_from_block_finishes(self):
        response = mock.Mock()
        response.status_code = 200
        response.text = """
            <html><body>
              <h1>Kitchen Knife Set with Block</h1>
              <script>
              const webItemsMap = {
                "1815C": {"displayedOptions": [
                  {"optionType": "Handle Color", "description": "Classic"},
                  {"optionType": "Block Finish", "description": "Cherry"}
                ]},
                "1815W": {"displayedOptions": [
                  {"optionType": "Handle Color", "description": "Pearl"},
                  {"optionType": "Block Finish", "description": "Natural"}
                ]}
              };
              </script>
            </body></html>
        """
        with mock.patch("scraping.requests.get", return_value=response):
            scrape_set_variant_options.cache_clear()
            self.assertEqual(
                scrape_set_variant_options("https://www.cutco.com/p/1815C", "1815"),
                {
                    "handle_colors": ("Classic", "Pearl"),
                    "block_finishes": ("Cherry", "Natural"),
                    "handle_colors_authoritative": True,
                },
            )

    def test_set_variant_options_ignore_holder_finish_for_tools_only_set(self):
        response = mock.Mock()
        response.status_code = 200
        response.text = """
            <html><head>
              <meta property="og:title" content="Kitchen Tool Sets with Holder">
            </head><body>
              <h1>#1719C</h1>
              <h1>5-Pc. Kitchen Tool Set (Tools Only)</h1>
              <script>
                const webItemsMap = {
                  "1719C": {"displayedOptions": [
                    {"optionType": "Handle Color", "description": "Classic"},
                    {"optionType": "Block Finish", "description": "Honey"}
                  ]},
                  "1718C": {"displayedOptions": [
                    {"optionType": "Block Finish", "description": "Cherry"}
                  ]}
                };
              </script>
            </body></html>
        """
        with mock.patch("scraping.requests.get", return_value=response):
            scrape_set_variant_options.cache_clear()
            self.assertEqual(
                scrape_set_variant_options(
                    "https://www.cutco.com/p/5-pc-kitchen-tool-set", "1719"
                ),
                {
                    "handle_colors": ("Classic",),
                    "block_finishes": (),
                    "handle_colors_authoritative": True,
                },
            )

    def test_set_variant_options_use_sku_storage_when_heading_is_generic(self):
        response = mock.Mock()
        response.status_code = 200
        response.text = """
            <html><body>
              <h1>Kitchen Tool Sets</h1>
              <script>
                const webItemsMap = {
                  "1718C": {
                    "itemName": "5-Pc. Kitchen Tool Set with Holder",
                    "itemOptions": [{
                      "optionType": "Tools Storage",
                      "displayedType": "Storage",
                      "optionCode": "With Holder",
                      "description": "With Holder"
                    }],
                    "displayedOptions": [
                      {"optionType": "Handle Color", "description": "Classic"},
                      {"optionType": "Block Finish", "description": "Honey"},
                      {"optionType": "Block Finish", "description": "Cherry"}
                    ]
                  },
                  "1719C": {
                    "itemName": "5-Pc. Kitchen Tool Set (Tools Only)",
                    "itemOptions": [{
                      "optionType": "Tools Storage",
                      "displayedType": "Storage",
                      "optionCode": "Tools Only",
                      "description": "Tools Only"
                    }]
                  }
                };
              </script>
            </body></html>
        """
        with mock.patch("scraping.requests.get", return_value=response):
            scrape_set_variant_options.cache_clear()
            self.assertEqual(
                scrape_set_variant_options(
                    "https://www.cutco.com/p/kitchen-tool-sets/1718C", "1718"
                ),
                {
                    "handle_colors": ("Classic",),
                    "block_finishes": ("Honey", "Cherry"),
                    "handle_colors_authoritative": True,
                },
            )

    def test_set_variant_options_ignore_family_handle_without_exact_sku(self):
        response = mock.Mock()
        response.status_code = 200
        response.text = """
            <html><body>
              <h1>6-Pc. Kitchen Tool Set with Holder</h1>
              <fieldset class="swatch-group Color" data-type="Handle Color">
                <div class="swatch product-option color-swatch" data-option="Classic"></div>
                <div class="swatch product-option color-swatch" data-option="Pearl"></div>
                <div class="swatch product-option color-swatch" data-option="Red"></div>
              </fieldset>
              <script>
                const webItemsMap = {
                  "1792C": {"itemOptions": [
                    {"optionType": "Tools Handle Color", "displayedType": "Color", "description": "Classic"}
                  ]},
                  "1792W": {"itemOptions": [
                    {"optionType": "Tools Handle Color", "displayedType": "Color", "description": "Pearl"}
                  ]},
                  "1718R": {"itemOptions": [
                    {"optionType": "Tools Handle Color", "displayedType": "Color", "description": "Red"}
                  ]}
                };
              </script>
            </body></html>
        """
        with mock.patch("scraping.requests.get", return_value=response):
            scrape_set_variant_options.cache_clear()
            self.assertEqual(
                scrape_set_variant_options(
                    "https://www.cutco.com/p/kitchen-tool-sets/1792C", "1792"
                ),
                {
                    "handle_colors": ("Classic", "Pearl"),
                    "block_finishes": (),
                    "handle_colors_authoritative": True,
                },
            )

    def test_discovers_product_page_url_by_exact_sku_from_category_pages(self):
        from scraping import (
            _cutco_product_url_lookup,
            discover_cutco_item_page_url,
        )

        response = mock.Mock()
        response.text = """
            <html><body>
              <a href="/p/traditional-flatware-accessories/1570W">Accessories</a>
              <a href="/p/traditional-flatware-accessories/1570C">Accessories</a>
            </body></html>
        """
        response.raise_for_status.return_value = None

        _cutco_product_url_lookup.cache_clear()
        with (
            mock.patch(
                "scraping._build_category_list",
                return_value=[("Flatware", "https://www.cutco.com/shop/flatware")],
            ),
            mock.patch("scraping.requests.get", return_value=response),
        ):
            self.assertEqual(
                discover_cutco_item_page_url("1570W"),
                "https://www.cutco.com/p/traditional-flatware-accessories/1570W",
            )
            self.assertEqual(
                discover_cutco_item_page_url("1570"),
                "https://www.cutco.com/p/traditional-flatware-accessories/1570W",
            )
        _cutco_product_url_lookup.cache_clear()

    def test_dedupe_product_links_prefers_named_duplicate_anchors(self):
        soup = BeautifulSoup(
            """
            <html><body>
              <a href="/p/4135-2" class="tb-pic"></a>
              <a href="/p/4135-2&view=product" class="tb-product-details">
                <h3>Cutco 4&quot; Vegetable Knife Sheath</h3>
              </a>
            </body></html>
            """,
            "html.parser",
        )
        deduped = _dedupe_product_links(soup.select("a[href*='/p/']"))
        self.assertEqual(len(deduped), 1)
        _anchor, href, name = deduped[0]
        self.assertEqual(href, "/p/4135-2&view=product")
        self.assertEqual(name, 'Cutco 4" Vegetable Knife Sheath')

    def test_product_link_name_prefers_title_over_full_text(self):
        soup = BeautifulSoup(
            """
            <a href="/p/5-pc-garden-tool-set&view=product" class="tb-product-details">
              <h3>5-Pc. Garden Tool Set w/FREE Garden Bag</h3>
              <span>Cutco 5-Pc. Garden Tool Set w/FREE Garden Bag $23 Set Savings $310 $333 When Purchased Separately Your garden will flourish with this complete set of garden tools.</span>
            </a>
            """,
            "html.parser",
        )
        anchor = soup.find("a")
        self.assertEqual(
            _product_link_name(anchor), "5-Pc. Garden Tool Set w/FREE Garden Bag"
        )

    def test_should_queue_slug_allows_sheaths_to_override_seen_urls(self):
        seen = {"https://www.cutco.com/p/4135-2&view=product"}
        self.assertTrue(
            _should_queue_slug(
                "https://www.cutco.com/p/4135-2&view=product", "Sheaths", seen
            ),
        )
        self.assertFalse(
            _should_queue_slug(
                "https://www.cutco.com/p/4135-2&view=product", "Storage", seen
            ),
        )

    def test_member_hover_titles_trim_set_lists(self):
        self.assertEqual(
            _member_hover_title("Barbecue Tongs, Barbecue Turner, Barbecue Fork"),
            "Barbecue Tongs",
        )
        self.assertEqual(
            _member_hover_title("Super Shears - 77, 78"),
            "Super Shears",
        )
        self.assertEqual(
            _member_hover_title(
                "Basting Spoon Slotted Spoon Ladle Mix-Stir Kitchen Tool Holder"
            ),
            "Basting Spoon",
        )
        self.assertEqual(_member_hover_title("Super Shears"), "Super Shears")
        self.assertIsNone(_member_hover_title(""))

    def test_infer_visible_member_sku_supports_gift_box_pages(self):
        response = mock.Mock()
        response.status_code = 200
        response.text = """
            <html><body><h1>#2026D</h1><h1>Gift Box for Super Shears</h1></body></html>
        """
        with mock.patch("scraping.requests.get", return_value=response):
            self.assertEqual(
                _infer_visible_member_sku("Gift Box for Super Shears"), "2026D"
            )
            self.assertIsNone(_infer_visible_member_sku("Super Shears"))

    def test_infer_visible_member_sku_supports_sheath_pages(self):
        response = mock.Mock()
        response.status_code = 200
        response.text = """
            <html><body><h1>#2120-2</h1><h1>4\" Paring Knife Sheath</h1></body></html>
        """
        with mock.patch("scraping.requests.get", return_value=response):
            self.assertEqual(
                _infer_visible_member_sku('4" Paring Knife Sheath'), "2120-2"
            )

    def test_infer_visible_member_sku_supports_generic_box_rows(self):
        response = mock.Mock()
        response.status_code = 200
        response.text = """
            <html><body><h1>#2130CD</h1><h1>Wine & Cheese Set</h1></body></html>
        """
        with mock.patch("scraping.requests.get", return_value=response):
            self.assertEqual(
                _infer_visible_member_sku(
                    "Gift Box",
                    context_url="https://www.cutco.com/p/wine-cheese-gift-set",
                ),
                "2130CD",
            )

    def test_resolve_visible_member_sku_prefers_linked_product_pages(self):
        from scraping import _fetch_sku_from_page

        _fetch_sku_from_page.cache_clear()
        parent_response = mock.Mock()
        parent_response.status_code = 200
        parent_response.text = """
            <html><body><h1>#1820</h1><h1>Salad Mates</h1></body></html>
        """
        child_response = mock.Mock()
        child_response.status_code = 200
        child_response.text = """
            <html><body><h1>#1720C</h1><h1>2-3/4&quot; Paring Knife</h1></body></html>
        """
        with mock.patch(
            "scraping.requests.get", side_effect=[parent_response, child_response]
        ):
            self.assertEqual(
                _resolve_visible_member_sku(
                    [
                        "https://www.cutco.com/p/salad-mates",
                        "https://www.cutco.com/p/paring-knife",
                    ],
                    '2-3/4" Paring Knife',
                    context_url="https://www.cutco.com/p/salad-mates",
                    set_sku="1820",
                ),
                "1720C",
            )

    def test_collect_visible_set_piece_rows_uses_data_item(self):
        soup = BeautifulSoup(
            """
            <ul>
              <li class="pdp-piece">
                <a class="pdp-set-item-detail" data-item="paring-knife" data-item-selected="1720C" href="#">
                  <img alt='2-3/4" Paring Knife' src="https://images.cutco.com/products/rolo/1720C-h.jpg?width=800"/>
                  <span class="pdp-use-detail">2-3/4" Paring Knife </span>
                </a>
              </li>
              <li class="pdp-piece">
                <a class="pdp-set-item-detail" data-item="trimmer" data-item-selected="1721C" href="#">
                  <img alt="Trimmer" src="https://images.cutco.com/products/rolo/1721C-h.jpg?width=800"/>
                  <span class="pdp-use-detail">Trimmer </span>
                </a>
              </li>
              <li class="pdp-piece pdp-piece-no-details" data-length="-1">
                <img alt="Gift Box" src="https://images.cutco.com/products/rolo/2111D-h.jpg?width=800"/>
                <span class="pdp-use-detail">Gift Box </span>
              </li>
            </ul>
            """,
            "html.parser",
        )
        rows = _collect_visible_set_piece_rows(
            soup.ul, context_url="https://www.cutco.com/p/salad-mates", set_sku="1820CD"
        )
        self.assertEqual([row["sku"] for row in rows[:2]], ["1720", "1721"])
        self.assertEqual(
            [row["name"] for row in rows[:2]], ['2-3/4" Paring Knife', "Trimmer"]
        )
        self.assertEqual(rows[2]["sku"], "2111D")

    def test_build_set_member_entries_uses_visible_row_skus(self):
        structured_members = [{"sku": "777", "name": "Super Shears", "quantity": 1}]
        visible_rows = [
            {"name": "Super Shears", "sku": "777", "is_set_only": False},
            {"name": "Gift Box", "sku": "123", "is_set_only": True},
        ]

        member_entries = _build_set_member_entries(
            structured_members,
            visible_rows,
            ["777", "123"],
            {"777": 1, "123": 1},
        )

        self.assertEqual(member_entries[0]["sku"], "777")
        self.assertEqual(member_entries[0]["name"], "Super Shears")
        self.assertEqual(member_entries[1]["sku"], "123")
        self.assertEqual(member_entries[1]["name"], "Gift Box")

    def test_time_utils_format_in_container_timezone(self):
        with mock.patch.dict(os.environ, {"TZ": "America/Boise"}, clear=False):
            tz, tz_name = container_timezone()
            self.assertEqual(tz_name, "America/Boise")
            self.assertEqual(format_container_time(None), "—")
            self.assertEqual(format_container_time("not-a-time"), "not-a-time")
            self.assertEqual(
                format_container_time("2026-04-20T19:18:00+00:00"),
                "Apr 20, 2026, 1:18 PM MDT",
            )
            self.assertEqual(
                format_container_time("2026-04-20T19:18:00"),
                "Apr 20, 2026, 1:18 PM MDT",
            )

        self.assertEqual(tz.key, "America/Boise")

    def test_time_utils_invalid_timezone_falls_back_to_utc(self):
        with mock.patch.dict(os.environ, {"TZ": "Not/AZone"}, clear=False):
            tz, tz_name = container_timezone()
            self.assertEqual(tz_name, "UTC")
            self.assertEqual(
                format_container_time("2026-04-20T19:18:00+00:00"),
                "Apr 20, 2026, 7:18 PM UTC",
            )
