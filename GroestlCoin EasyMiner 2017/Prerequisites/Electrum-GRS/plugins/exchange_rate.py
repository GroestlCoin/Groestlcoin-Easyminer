from PyQt4.QtGui import *
from PyQt4.QtCore import *

from datetime import datetime
import inspect
import requests
import sys
from threading import Thread
import time
import traceback
import csv
from decimal import Decimal
from functools import partial

from electrum_grs.bitcoin import COIN
from electrum_grs.plugins import BasePlugin, hook
from electrum_grs.i18n import _
from electrum_grs.util import PrintError, ThreadJob, timestamp_to_datetime
from electrum_grs.util import format_satoshis
from electrum_grs_gui.qt.util import *
from electrum_grs_gui.qt.amountedit import AmountEdit

# See https://en.wikipedia.org/wiki/ISO_4217
CCY_PRECISIONS = {'BHD': 3, 'BIF': 0, 'BYR': 0, 'CLF': 4, 'CLP': 0,
                  'CVE': 0, 'DJF': 0, 'GNF': 0, 'IQD': 3, 'ISK': 0,
                  'JOD': 3, 'JPY': 0, 'KMF': 0, 'KRW': 0, 'KWD': 3,
                  'LYD': 3, 'MGA': 1, 'MRO': 1, 'OMR': 3, 'PYG': 0,
                  'RWF': 0, 'TND': 3, 'UGX': 0, 'UYI': 0, 'VND': 0,
                  'VUV': 0, 'XAF': 0, 'XAG': 2, 'XAU': 4, 'XOF': 0,
                  'XPF': 0, 'BTC': 8}

class ExchangeBase(PrintError):
    def __init__(self, sig):
        self.history = {}
        self.quotes = {}
        self.sig = sig

    def protocol(self):
        return "https"

    def get_json(self, site, get_string):
        url = "".join([self.protocol(), '://', site, get_string])
        response = requests.request('GET', url,
                                    headers={'User-Agent' : 'Electrum'})
        return response.json()

    def get_csv(self, site, get_string):
        url = "".join([self.protocol(), '://', site, get_string])
        response = requests.request('GET', url,
                                    headers={'User-Agent' : 'Electrum'})
        reader = csv.DictReader(response.content.split('\n'))
        return list(reader)

    def name(self):
        return self.__class__.__name__

    def update_safe(self, ccy):
        try:
            self.print_error("getting fx quotes for", ccy)
            self.quotes = self.get_rates(ccy)
            self.print_error("received fx quotes")
            self.sig.emit(SIGNAL('fx_quotes'))
        except Exception, e:
            self.print_error("failed fx quotes:", e)

    def update(self, ccy):
        t = Thread(target=self.update_safe, args=(ccy,))
        t.setDaemon(True)
        t.start()

    def get_historical_rates_safe(self, ccy):
        try:
            self.print_error("requesting fx history for", ccy)
            self.history[ccy] = self.historical_rates(ccy)
            self.print_error("received fx history for", ccy)
            self.sig.emit(SIGNAL("fx_history"))
        except Exception, e:
            self.print_error("failed fx history:", e)

    def get_historical_rates(self, ccy):
        result = self.history.get(ccy)
        if not result and ccy in self.history_ccys():
            t = Thread(target=self.get_historical_rates_safe, args=(ccy,))
            t.setDaemon(True)
            t.start()
        return result

    def history_ccys(self):
        return []

    def historical_rate(self, ccy, d_t):
        return self.history.get(ccy, {}).get(d_t.strftime('%Y-%m-%d'))


class BlockchainInfo(ExchangeBase):
    def get_rates(self, ccy):
        json = self.get_json('blockchain.info', '/ticker')
        return dict([(r, Decimal(json[r]['15m'])) for r in json])

    def name(self):
        return "Blockchain"

class Poloniex(ExchangeBase):
    def get_rates(self, ccy):
        quote_currencies = {}
        tickers = self.get_json('poloniex.com', '/public?command=returnTicker')
        grs_ticker = tickers.get('BTC_GRS')
        grs_btc_rate = quote_currencies['BTC'] = Decimal(grs_ticker['last'])

        blockchain_tickers = BlockchainInfo(self.sig).get_rates(ccy)
        for currency, btc_rate in blockchain_tickers.items():
            quote_currencies[currency] = grs_btc_rate * btc_rate

        return quote_currencies


class Plugin(BasePlugin, ThreadJob):

    def __init__(self, parent, config, name):
        BasePlugin.__init__(self, parent, config, name)
        # Signal object first
        self.sig = QObject()
        self.sig.connect(self.sig, SIGNAL('fx_quotes'), self.on_fx_quotes)
        self.sig.connect(self.sig, SIGNAL('fx_history'), self.on_fx_history)
        self.ccy = self.config_ccy()
        self.history_used_spot = False
        self.ccy_combo = None
        self.hist_checkbox = None
        self.windows = dict()

        is_exchange = lambda obj: (inspect.isclass(obj)
                                   and issubclass(obj, ExchangeBase)
                                   and obj != ExchangeBase
                                   and obj != BlockchainInfo)
        self.exchanges = dict(inspect.getmembers(sys.modules[__name__],
                                                 is_exchange))
        self.set_exchange(self.config_exchange())

    def ccy_amount_str(self, amount, commas):
        prec = CCY_PRECISIONS.get(self.ccy, 2)
        fmt_str = "{:%s.%df}" % ("," if commas else "", max(0, prec))
        return fmt_str.format(round(amount, prec))

    def thread_jobs(self):
        return [self]

    def run(self):
        # This runs from the network thread which catches exceptions
        if self.windows and self.timeout <= time.time():
            self.timeout = time.time() + 150
            self.exchange.update(self.ccy)

    def config_ccy(self):
        '''Use when dynamic fetching is needed'''
        return self.config.get("currency", "BTC")

    def config_exchange(self):
        return self.config.get('use_exchange', 'Poloniex')

    def config_history(self):
        return self.config.get('history_rates', 'unchecked') != 'unchecked'

    def show_history(self):
        return self.config_history() and self.exchange.history_ccys()

    def set_exchange(self, name):
        class_ = self.exchanges.get(name) or self.exchanges.values()[0]
        name = class_.__name__
        self.print_error("using exchange", name)
        if self.config_exchange() != name:
            self.config.set_key('use_exchange', name, True)
        self.exchange = class_(self.sig)
        # A new exchange means new fx quotes, initially empty.  Force
        # a quote refresh
        self.timeout = 0
        self.get_historical_rates()
        self.on_fx_quotes()

    def update_status_bars(self):
        '''Update status bar fiat balance in all windows'''
        for window in self.windows:
            window.update_status()

    def on_new_window(self, window):
        # Additional send and receive edit boxes
        send_e = AmountEdit(self.config_ccy)
        window.send_grid.addWidget(send_e, 4, 2, Qt.AlignLeft)
        window.amount_e.frozen.connect(
            lambda: send_e.setFrozen(window.amount_e.isReadOnly()))
        receive_e = AmountEdit(self.config_ccy)
        window.receive_grid.addWidget(receive_e, 2, 2, Qt.AlignLeft)

        self.windows[window] = {'edits': (send_e, receive_e),
                                'last_edited': {}}
        self.connect_fields(window, window.amount_e, send_e, window.fee_e)
        self.connect_fields(window, window.receive_amount_e, receive_e, None)
        window.history_list.refresh_headers()
        window.update_status()

    def connect_fields(self, window, btc_e, fiat_e, fee_e):
        last_edited = self.windows[window]['last_edited']

        def edit_changed(edit):
            edit.setStyleSheet(BLACK_FG)
            last_edited[(fiat_e, btc_e)] = edit
            amount = edit.get_amount()
            rate = self.exchange_rate()
            if rate is None or amount is None:
                if edit is fiat_e:
                    btc_e.setText("")
                    if fee_e:
                        fee_e.setText("")
                else:
                    fiat_e.setText("")
            else:
                if edit is fiat_e:
                    btc_e.setAmount(int(amount / Decimal(rate) * COIN))
                    if fee_e: window.update_fee()
                    btc_e.setStyleSheet(BLUE_FG)
                else:
                    fiat_e.setText(self.ccy_amount_str(
                        amount * Decimal(rate) / COIN, False))
                    fiat_e.setStyleSheet(BLUE_FG)

        fiat_e.textEdited.connect(partial(edit_changed, fiat_e))
        btc_e.textEdited.connect(partial(edit_changed, btc_e))
        last_edited[(fiat_e, btc_e)] = btc_e

    @hook
    def do_clear(self, window):
        self.windows[window]['edits'][0].setText('')

    def on_close_window(self, window):
        self.windows.pop(window)

    def close(self):
        # Get rid of hooks before updating status bars.
        BasePlugin.close(self)
        self.update_status_bars()
        self.refresh_headers()
        for window, data in self.windows.items():
            for edit in data['edits']:
                edit.hide()
            window.update_status()

    def refresh_headers(self):
        for window in self.windows:
            window.history_list.refresh_headers()

    def on_fx_history(self):
        '''Called when historical fx quotes are updated'''
        for window in self.windows:
            window.history_list.update()

    def on_fx_quotes(self):
        '''Called when fresh spot fx quotes come in'''
        self.update_status_bars()
        self.populate_ccy_combo()
        # Refresh edits with the new rate
        for window, data in self.windows.items():
            for edit in data['last_edited'].values():
                edit.textEdited.emit(edit.text())
        # History tab needs updating if it used spot
        if self.history_used_spot:
            self.on_fx_history()

    def on_ccy_combo_change(self):
        '''Called when the chosen currency changes'''
        ccy = str(self.ccy_combo.currentText())
        if ccy and ccy != self.ccy:
            self.ccy = ccy
            self.config.set_key('currency', ccy, True)
            self.update_status_bars()
            self.get_historical_rates() # Because self.ccy changes
            self.hist_checkbox_update()

    def hist_checkbox_update(self):
        if self.hist_checkbox:
            self.hist_checkbox.setEnabled(self.ccy in self.exchange.history_ccys())
            self.hist_checkbox.setChecked(self.config_history())

    def populate_ccy_combo(self):
        # There should be at most one instance of the settings dialog
        combo = self.ccy_combo
        # NOTE: bool(combo) is False if it is empty.  Nuts.
        if combo is not None:
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(sorted(self.exchange.quotes.keys()))
            combo.blockSignals(False)
            combo.setCurrentIndex(combo.findText(self.ccy))

    def exchange_rate(self):
        '''Returns None, or the exchange rate as a Decimal'''
        rate = self.exchange.quotes.get(self.ccy)
        if rate:
            return Decimal(rate)

    @hook
    def format_amount_and_units(self, btc_balance):
        rate = self.exchange_rate()
        return '' if rate is None else " (%s %s)" % (self.value_str(btc_balance, rate), self.ccy)

    @hook
    def get_fiat_status_text(self, btc_balance):
        rate = self.exchange_rate()
        return _("  (No FX rate available)") if rate is None else "1 GRS~%s %s" % (self.value_str(COIN, rate), self.ccy)

    def get_historical_rates(self):
        if self.show_history():
            self.exchange.get_historical_rates(self.ccy)

    def requires_settings(self):
        return True

    def value_str(self, satoshis, rate):
        if satoshis is None:  # Can happen with incomplete history
            return _("Unknown")
        if rate:
            value = Decimal(satoshis) / COIN * Decimal(rate)
            return "%s" % (self.ccy_amount_str(value, True))
        return _("No data")

    @hook
    def historical_value_str(self, satoshis, d_t):
        rate = self.exchange.historical_rate(self.ccy, d_t)
        # Frequently there is no rate for today, until tomorrow :)
        # Use spot quotes in that case
        if rate is None and (datetime.today().date() - d_t.date()).days <= 2:
            rate = self.exchange.quotes.get(self.ccy)
            self.history_used_spot = True
        return self.value_str(satoshis, rate)

    @hook
    def history_tab_headers(self, headers):
        if self.show_history():
            headers.extend(['%s '%self.ccy + _('Amount'), '%s '%self.ccy + _('Balance')])

    @hook
    def history_tab_update_begin(self):
        self.history_used_spot = False

    @hook
    def history_tab_update(self, tx, entry):
        if not self.show_history():
            return
        tx_hash, conf, value, timestamp, balance = tx
        if conf <= 0:
            date = datetime.today()
        else:
            date = timestamp_to_datetime(timestamp)
        for amount in [value, balance]:
            text = self.historical_value_str(amount, date)
            entry.append(text)

    def settings_widget(self, window):
        return EnterButton(_('Settings'), self.settings_dialog)

    def settings_dialog(self):
        d = QDialog()
        d.setWindowTitle("Settings")
        layout = QGridLayout(d)
        layout.addWidget(QLabel(_('Exchange rate API: ')), 0, 0)
        layout.addWidget(QLabel(_('Currency: ')), 1, 0)
        # Disabled.
        # layout.addWidget(QLabel(_('History Rates: ')), 2, 0)

        # Currency list
        self.ccy_combo = QComboBox()
        self.ccy_combo.currentIndexChanged.connect(self.on_ccy_combo_change)
        self.populate_ccy_combo()

        def on_change_ex(idx):
            exchange = str(combo_ex.currentText())
            if exchange != self.exchange.name():
                self.set_exchange(exchange)
                self.hist_checkbox_update()

        def on_change_hist(checked):
            if checked:
                self.config.set_key('history_rates', 'checked')
                self.get_historical_rates()
            else:
                self.config.set_key('history_rates', 'unchecked')
            self.refresh_headers()

        def ok_clicked():
            self.timeout = 0
            self.ccy_combo = None
            d.accept()

        combo_ex = QComboBox()
        combo_ex.addItems(sorted(self.exchanges.keys()))
        combo_ex.setCurrentIndex(combo_ex.findText(self.config_exchange()))
        combo_ex.currentIndexChanged.connect(on_change_ex)

        self.hist_checkbox = QCheckBox()
        self.hist_checkbox.stateChanged.connect(on_change_hist)
        self.hist_checkbox_update()

        ok_button = QPushButton(_("OK"))
        ok_button.clicked.connect(lambda: ok_clicked())

        layout.addWidget(self.ccy_combo,1,1)
        layout.addWidget(combo_ex,0,1)
        # Disabled.
        # layout.addWidget(self.hist_checkbox,2,1)
        layout.addWidget(ok_button,3,1)

        return d.exec_()
