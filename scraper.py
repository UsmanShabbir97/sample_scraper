# -*- coding: utf-8 -*-
import re
import time

from selenium.common.exceptions import NoSuchElementException, WebDriverException
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By

from scrapers.parsers.base import BaseSpider, PlaceOrderMixin, SeleniumMixin
from utils.dates import next_business_days


class Scraper(SeleniumMixin, PlaceOrderMixin, BaseSpider):
    DOMAIN = 'https://www.xxxxxxxxxxxxxxxx.com'

    # Necessary for filling the item table in the order details window.
    # because there are multiple windows and clicking the element doesn't work properly
    JSCODE = '''
        (function () {
            var fields = ["product","ZZBISMT","MAKTG","quantity","NAME2","VSTEL","plant",
                        "LAND1","AVAILQTY","MEINS","UNIT_PRICE","LIST_PRICE","DZEIT",
                        "NAME1","STRAS","ORT01","REGIO","PSTLZ"];
            var formObject = opener.document.forms["order_positions"];
            var fieldObject;
            for(var i=0;i<arguments.length - 1; i++){
                field_name = fields[i] +"\x5b"+ arguments[arguments.length - 1] + "\x5d";
                fieldObject = formObject.elements[field_name];
                if (fieldObject != null) {
                  fieldObject.value = arguments[i];
                }
            }
        })(
    '''

    def login(self):
        self.browser.get(self.DOMAIN + '/b2b_altra/b2b/init.do?scenario.xcm=ALTRA')
        try:
            self.browser.find_element_by_name("UserId").send_keys(self.USERNAME)
            self.browser.find_element_by_name("nolog_password").send_keys(self.PASSWORD)
            self.browser.find_element_by_name("login").click()
            # they added even more iframes
            self.switch_to_frame_by_attr('name', frame_path='isaTop/header')
            WebDriverWait(self.browser, 15).until(
                EC.element_to_be_clickable((By.XPATH, ".//*[contains(., 'Log off')]")))
            self.logged_in = True
            return True
        except Exception:
            self.log.exception("failed to login")
            return False

    def search_product(self, product, input_name):
        self.browser.find_element_by_name('product[1]').clear()
        self.browser.find_element_by_name('MAKTG[1]').clear()
        self.fill_input_by_attr('name', attr_value=input_name, text=product)
        self.click_link_by_attr('.', attr_value='Search', exact=True)
        return self.browser.find_elements_by_xpath("//table[@class='itemlist']//tr")

    def get_availability(self, catalog_number, **kwargs):
        if not self.logged_in and not self.login():
            return {}, 0, False
        availability, price, lead_date = {}, 0, None
        self.browser.get(self.DOMAIN + '/b2b_altra/base/helpvalues.do?'
                                       'helpValuesSearch=Product&KUNNR[1]=0000002694&parameterIndex=1')
        rows = self.search_product(catalog_number, 'product[1]')
        # search by descr might work for some products
        if not rows:
            rows = self.search_product(catalog_number, 'MAKTG[1]')
        for row in rows[1:]:
            try:
                qty = self.clean_qty(row.find_element_by_xpath("./td[7]/a").text)
                location = row.find_element_by_xpath("./td[14]/a").text
                lead_days = int(row.find_element_by_xpath("./td[11]/a").text)
                if lead_days:
                    lead_date = next_business_days(lead_days)[-1][0]
                availability[location.strip()] = {
                    'qty': qty,
                    'lead_date': lead_date if lead_date and not qty else None
                }
                price = row.find_element_by_xpath("./td[9]/a")
                price = self.clean_price(price.text)
            except (NoSuchElementException, ValueError):
                self.log.exception("avail processing error")
                continue

        return availability, price, True

    def replace_catalog_numbers(self, key, results):
        '''
        Replace new catalog numbers with old ones
        '''
        self.browser.get(self.DOMAIN + '/b2b_altra/base/helpvalues.do?'
                                       'helpValuesSearch=Product&KUNNR[1]=0000002694&parameterIndex=1')
        for result in results:
            try:
                self.fill_input_by_attr('name', attr_value='product[1]', text=result[key])
                self.click_link_by_attr('.', attr_value='Search', exact=True)
                result[key] = self.browser.find_element_by_xpath(
                    "//table[@class='itemlist']//tr[2]/td[2]/a").text.strip()
            except WebDriverException:
                self.log.exception("failed to replace catalog number")
                continue

    def get_carrier_from_string(self, ship_data):
        if '.ups.com' in ship_data:
            carrier = 'UPS'
        elif 'fedex.com' in ship_data:
            carrier = 'FEDEX'
        elif 'rlcarriers.com' in ship_data:
            carrier = 'R&L CARRIERS'
        else:
            carrier = ''
        return carrier

    def search_po(self, order_number, **kwargs):
        if not self.logged_in and not self.login():
            return False
        try:
            self.browser.get(self.DOMAIN + '/b2b_altra/genericsearch.do?genericsearch.name=SearchCriteria_B2B_Sales'
                                           '&genericsearch.start=true&GSdateformat=mm/dd/yyyy'
                                           '&GSnumberformat=%23%2c%23%230.%23%23%23&GSlanguage=EN'
                                           '&GSdocumenthandlernoadd=&rc_documenttypes=ORDER&rc_status_head1='
                                           '&rc_attributesUI=last_year&rc_datetoken5=last_year'
                                           '&rc_attsubcharUI=PURCHASE_ORDER&rc_po_number_uc=' + order_number)
            link = self.browser.find_element_by_xpath("//table[@summary='Search Results']"
                                                       "//tr[./td[contains(., '%s')]]//a" % order_number)
            link.click()
        except WebDriverException:
            self.log.info("failed to find PO #")
            return False
        return True

    def get_tracking(self, order_number, **kwargs):
        if not self.search_po(order_number):
            return []
        # the page with the order data
        self.browser.get(self.DOMAIN + '/b2b_altra/ecombase/documentstatus/orderstatusdetail.jsp')
        results = []
        rows = self.browser.find_elements_by_xpath("//table[@class='itemlist']//tr[contains(@id, 'row_')]")
        rows_detail = self.browser.find_elements_by_xpath("//table[@class='itemlist']//tr[contains(@id, 'rowdetail_')]")
        reg_expr = re.compile("&(?:InquiryNumber|tracknumbers)=(\w+)")
        for i, row in enumerate(rows):
            try:
                product = row.find_element_by_xpath("./td[@class='product']")
                date_elem = row.find_element_by_xpath("./td[@class='date-on']")
                ship_date = self.format_date(date_elem.text.split()[0])
                qty_elem = row.find_element_by_xpath("./td[@class='qty']")
                qty = qty_elem.text.split()
                track_elem = rows_detail[i].find_element_by_xpath(".//a[./img[@alt='External Order Tracking']]")
                track_elem = track_elem.get_attribute('onclick')
                tracking = re.search(reg_expr, track_elem).groups(0)[0]
            except (NoSuchElementException, IndexError, ValueError):
                self.log.exception("order details processing error")
                track_elem, tracking, shipping_method = '', '', ''
            shipping_method = self.get_carrier_from_string(track_elem)
            result_dictionary = {
                'item_id': product.text.strip(),
                'status': 'not shipped' if not tracking else 'shipped',
                'tracking_number': tracking,
                'shipping_method': shipping_method,
                'ship_date': ship_date,
                'qty': self.clean_qty(qty),
            }
            results.append(result_dictionary)

        try:
            cost = self.browser.find_element_by_xpath(
                ".//td[contains(text(),'Shipping Costs:')]/following-sibling::td[1]").text
            cost = self.clean_price(cost)
        except NoSuchElementException:
            cost = 0
        if results and cost:
            results[0]['shipping_cost'] = cost
        self.replace_catalog_numbers('item_id', results)
        return results

    def get_confirmation(self, order_number, **kwargs):
        if not self.search_po(order_number):
            return []
        results = []
        # the page with the order data
        self.browser.get(self.DOMAIN + '/b2b_altra/ecombase/documentstatus/orderstatusdetail.jsp')
        try:
            confirm_number = self.browser.find_element_by_xpath("//h1[contains(., 'Order:')]").text
            confirm_number = confirm_number.split()[1].strip()
        except (NoSuchElementException, IndexError):
            self.log.exception('Not found confirmation number')
            confirm_number = ""
        # get an address
        try:
            self.click_link_by_attr('onclick', attr_value='showShipTo')
            self.browser.switch_to_window(self.browser.window_handles[-1])
            address = []
            for name in ['lastName', 'firstName', 'street']:
                address.append(self.browser.find_element_by_name(name).get_attribute('value'))
            postal_code = self.browser.find_element_by_name('postalCode').get_attribute('value')
            city = self.browser.find_element_by_name('city').get_attribute('value')
            country = Select(self.browser.find_element_by_name('country'))
            country = country.first_selected_option.text.strip()
            state = Select(self.browser.find_element_by_name('region'))
            state = state.first_selected_option.text.strip()
            address.append(' '.join([city, state, postal_code, country]))
            address = '\n'.join(address)
            self.browser.switch_to_window(self.browser.window_handles[0])
        except WebDriverException:
            self.log.exception('Not found address')
            address = ""
        # get a shipping method
        try:
            track_elem = self.browser.find_element_by_xpath("//a[./img[@alt='External Order Tracking']]")
            track_elem = track_elem.get_attribute('onclick')
        except WebDriverException:
            self.log.exception('Not found shipping method')
            shipping_method = ''
        else:
            shipping_method = self.get_carrier_from_string(track_elem)
        res = {
            "address": address,
            "confirm_number": confirm_number,
            "shipping_method": shipping_method,
            "items": []
        }
        rows = self.browser.find_elements_by_xpath("//table[@class='itemlist']//tr[contains(@id, 'row_')]")
        for row in rows:
            try:
                item_id = row.find_element_by_xpath("./td[@class='product']").text
                date_elem = row.find_element_by_xpath("./td[@class='date-on']")
                ship_date = self.format_date(date_elem.text.split()[0])
                qty = row.find_element_by_xpath("./td[@class='qty']").text
            except NoSuchElementException:
                self.log.exception("order details processing error")
                continue
            result_dictionary = {
                'catalog_number': item_id,
                'estimated_ship_date': ship_date,
                'qty': self.clean_qty(qty.split()),
            }
            res['items'].append(result_dictionary)

        self.replace_catalog_numbers('catalog_number', res['items'])
        results.append(res)
        return results

    def cart_empty(self, empty=False):
        """Verify if cart is empty. Empty it if desired. Return boolean."""
        return True

    def put_items_in_cart(self, items):
        # create cart
        self.switch_to_frame_by_attr('name', frame_path='isaTop/work_history/form_input')
        self.click_link_by_attr('onclick', attr_value='create_order')
        # extend table of items
        size_dropdown = WebDriverWait(self.browser, self.DELAY).until(
            EC.visibility_of_element_located((By.ID, 'newposcount')))
        Select(size_dropdown).select_by_value('15')
        time.sleep(2)
        self.click_link_by_attr('onclick', attr_value='submit_refresh')
        return True

    def choose_closest_warehouses(self, x, items, detailed_availability):
        self.click_link_by_attr('onclick', attr_value='getHelpValuesPopupProduct')
        self.browser.switch_to_window(self.browser.window_handles[-1])
        weight_per_warehouse, ordered_items = {}, {}
        index = 1
        for item in items:
            ordered_items[item['catalog_number']] = {}
            avail_product = detailed_availability[item['catalog_number']]
            qty, weight = item['qty'], item['weight']
            qty_ordered = 0
            self.fill_input_by_attr('name', attr_value='product', text=item['catalog_number'])
            self.click_link_by_attr('.', attr_value='Search')
            # choose warehouses
            for elem in avail_product:
                location = elem["location_code"]
                avail_qty = elem["qty_num"]
                needed_qty = avail_qty if avail_qty < qty else qty
                if not avail_qty:
                    continue
                # select the right warehouse
                xpath = "//table[@class='itemlist']//a[normalize-space(.)='{}']".format(location)
                try:
                    warehouse = self.browser.find_element_by_xpath(xpath)
                except NoSuchElementException:
                    continue
                item_info = re.findall(r"\'.+?\'", warehouse.get_attribute("href"))
                try:
                    # add new product id. It's necessary for later verification 
                    item['new_product_id'] = item_info[0].strip('\'')
                    item_info[3] = "'{}'".format(needed_qty)
                except IndexError:
                    return False, weight_per_warehouse, ordered_items
                # fill product field with JavaScript
                item_info.append(str(index))
                js_script = self.JSCODE + ','.join(item_info) + ');'
                self.browser.execute_script(js_script)
                # sum the weights per warehouse to choose the shipping method later
                try:
                    weight_per_warehouse[location] += weight * needed_qty
                except KeyError:
                    weight_per_warehouse[location] = weight * needed_qty
                # collect ordered items qty for later verification
                ordered_items[item['catalog_number']][location] = needed_qty
                qty_ordered += needed_qty
                qty -= needed_qty
                index += 1
                if qty <= 0:
                    break
            else:
                return False, weight_per_warehouse, ordered_items
        return True, weight_per_warehouse, ordered_items

    def fill_client_details(self, x, order_details, weight_per_warehouse):
        self.browser.switch_to_window(self.browser.window_handles[0])
        self.switch_to_frame_by_attr('name', frame_path='isaTop/work_history/form_input')
        self.click_link_by_attr('onclick', attr_value='newShipTo')
        # fill company and customer names
        if order_details['first_name'] or order_details['last_name']:
            customer_name = order_details['first_name'] + ' ' + order_details['last_name']
        else:
            customer_name = ''
        if order_details['company']:
            self.fill_input_by_attr('name', attr_value='lastName', text=order_details['company'])
            self.fill_input_by_attr('name', attr_value='firstName', text=customer_name)
        else:
            self.fill_input_by_attr('name', attr_value='lastName', text=customer_name)
            self.fill_input_by_attr('name', attr_value='firstName', text='')
        self.fill_input_by_attr('name', attr_value='street', text=order_details['address']['address_1'])
        self.fill_input_by_attr('name', attr_value='city', text=order_details['address']['city'])
        self.fill_input_by_attr('name', attr_value='postalCode', text=order_details['address']['postal_code'])
        self.fill_input_by_attr('name', attr_value='telephoneNumber', text='8886712883')
        self.fill_input_by_attr('name', attr_value='faxNumber', text='3239086064')
        # select country and state
        country = order_details['address']['country']
        select_country = Select(self.browser.find_element_by_name('country'))
        select_country.select_by_value(country)
        if country == 'US' or country == 'CA':
            select_state = Select(self.browser.find_element_by_name('region'))
            select_state.select_by_value(order_details['address']['state'])
        self.click_link_by_attr('onclick', attr_value='saveForm')
        # select shipment method
        self.fill_input_by_attr('name', attr_value='poNumber', text=order_details['order_id'])
        select_method = Select(self.browser.find_element_by_id('zFreightForwarder'))
        select_billing = Select(self.browser.find_element_by_id('incoterms1'))
        if any(weight_per_warehouse[location] >= 70 for location in weight_per_warehouse):
            select_method.select_by_value('UPGF')
            select_billing.select_by_value('TPC')
            self.fill_input_by_attr('name', attr_value='incoterms2', text=self.LTL_ACCOUNT, exact=True)
            comment = self.LTL_DETAILS
        else:
            select_method.select_by_value('FDX003')
            select_billing.select_by_value('TPC')
            self.fill_input_by_attr('name', attr_value='incoterms2', text=self.FEDEX_ACCOUNT, exact=True)
            comment = self.FEDEX_DETAILS
        self.click_link_by_attr('href', attr_value='toggleText(\"text_1\")')
        self.fill_input_by_attr('name', attr_value='textZ004', text=comment)
        self.click_link_by_attr('onclick', attr_value='submit_simulate')
        return True

    def verify_address(self, order_details, address_info):
        """Check, if the order details contains address info from verification page"""
        correct = True
        full_name = order_details['first_name'] + ' ' + order_details['last_name']
        verify_fields = [full_name, order_details['company'],
                         order_details['address']['address_1'], order_details["address"]["city"]]
        verify_string = ''.join([field.lower() for field in verify_fields])
        for elem in address_info:
            if elem.lower() not in verify_string:
                self.log.warning("order verification: address not found")
                correct = False
        return correct

    def verify_order_placed(self, x, order_details, ordered_items):
        """Review the order details and if what we ordered is what we needed. Return boolean"""
        xpath = "(//div[@class='header-itemdefault']//td[@class='value'])[1]"
        raw_address = self.browser.find_element_by_xpath(xpath).text.split('...')
        address_info = [el.strip(' .') for el in raw_address]
        correct = self.verify_address(order_details, address_info)
        verify_items = {}
        rows = self.browser.find_elements_by_xpath("//td[@class='product']/parent::tr")
        for row in rows:
            try:
                part = row.find_element_by_xpath(".//td[@class='product']").text.strip()
                qty = row.find_element_by_xpath(".//td[@class='qty']")
                qty = self.clean_qty(qty.text.split())
            except (NoSuchElementException, ValueError, IndexError):
                self.log.exception("product/qty processing error")
                return False, x
            try:
                verify_items[part] += qty
            except KeyError:
                verify_items[part] = qty
        for item in order_details["items"]:
            new_product_id = item["new_product_id"]
            cat_num = item["catalog_number"]
            if not (item["qty"] == verify_items[new_product_id] == sum(ordered_items[cat_num].values())):
                correct = False
                self.log.warning("order verification: wrong qty selected for product {0}".format(cat_num))
        return correct, x

    def submit_order(self, x):
        try:
            self.browser.find_element_by_name('termsAccepted').click()
            # PhantomJS can't handle confirm popup dialog 
            self.browser.execute_script("window.confirm = function(){return true;}")
            self.click_link_by_attr('onclick', attr_value='sendPressed')
            self.browser.find_element_by_xpath("//body[@class='confirmation']")
            # extract confirmation number from order page
            xpath = "(//table[@class='header-general']//td[@class='value'])[1]"
            confirmation_number = self.browser.find_element_by_xpath(xpath).text.strip()
            success = True
        except WebDriverException:
            self.log.exception("confirmation error: failed to confirm order")
            confirmation_number, success = '', False
        return success, confirmation_number

    def place_order(self, order_details, submit=False, **kwargs):
        """Fill the details for an order and submit it if desired.
        Return boolean status, confirmation #, dict with ordered items/warehouses."""
        try:
            return super(Scraper, self).place_order(order_details, submit, **kwargs)
        except WebDriverException:
            self.quit_browser()
            return False, "", {}
