"""Prezzi PUN e CSUD del mese"""
from datetime import date, timedelta, datetime
import holidays
from statistics import mean
import zipfile, io
from bs4 import BeautifulSoup
import xml.etree.ElementTree as et
from typing import Tuple

from aiohttp import ClientSession
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.helpers.event import async_track_point_in_time
import homeassistant.util.dt as dt_util

from .const import (
    DOMAIN,
    PUN_FASCIA_MONO,
    PUN_FASCIA_F1,
    PUN_FASCIA_F2,
    PUN_FASCIA_F3,
    CONF_SCAN_HOUR,
    CONF_ACTUAL_DATA_ONLY,
    COORD_EVENT,
    EVENT_UPDATE_FASCIA,
    EVENT_UPDATE_PUN
)

import logging
_LOGGER = logging.getLogger(__name__)

# Definisce i tipi di entità
PLATFORMS: list[str] = ["sensor"]

async def async_setup_entry(hass: HomeAssistant, config: ConfigEntry) -> bool:
    """Impostazione dell'integrazione da configurazione Home Assistant"""
    
    # Salva il coordinator nella configurazione
    coordinator = PUNDataUpdateCoordinator(hass, config)
    hass.data.setdefault(DOMAIN, {})[config.entry_id] = coordinator

    # Aggiorna immediatamente la fascia oraria corrente
    await coordinator.update_fascia()

    # Calcola la data della prossima esecuzione (all'ora definita di domani)
    next_update_pun = dt_util.now().replace(hour=coordinator.scan_hour,
                            minute=0, second=0, microsecond=0)
    if next_update_pun <= dt_util.now():
            # Se l'evento è già trascorso la esegue domani alla stessa ora
            next_update_pun = next_update_pun + timedelta(days=1)

    # Schedula la prossima esecuzione dell'aggiornamento PUN
    async_track_point_in_time(hass, coordinator.update_pun, next_update_pun)
    _LOGGER.debug('Prossimo aggiornamento web: %s', next_update_pun.strftime('%d/%m/%Y %H:%M:%S'))

    # Crea i sensori con la configurazione specificata
    hass.config_entries.async_setup_platforms(config, PLATFORMS)

    # Registra il callback di modifica opzioni
    config.async_on_unload(config.add_update_listener(update_listener))
    return True

async def async_unload_entry(hass: HomeAssistant, config: ConfigEntry) -> bool:
    """Rimozione dell'integrazione da Home Assistant"""
    
    # Scarica i sensori (disabilitando di conseguenza il coordinator)
    unload_ok = await hass.config_entries.async_unload_platforms(config, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(config.entry_id)

    return unload_ok

async def update_listener(hass: HomeAssistant, config: ConfigEntry) -> None:
    """Modificate le opzioni da Home Assistant"""

    # Recupera il coordinator
    coordinator = hass.data[DOMAIN][config.entry_id]

    # Aggiorna le impostazioni del coordinator dalle opzioni
    if config.options[CONF_SCAN_HOUR] != coordinator.scan_hour:
        # Modificata l'ora di scansione
        coordinator.scan_hour = config.options[CONF_SCAN_HOUR]

        # Calcola la data della prossima esecuzione (all'ora definita)
        next_update_pun = dt_util.now().replace(hour=coordinator.scan_hour,
                                minute=0, second=0, microsecond=0)
        if next_update_pun.hour < dt_util.now().hour:
            # Se l'ora impostata è minore della corrente, schedula a domani
            # (perciò se è uguale esegue subito l'aggiornamento)
            next_update_pun = next_update_pun + timedelta(days=1)

        # Schedula la prossima esecuzione
        coordinator.web_retries = 0
        async_track_point_in_time(coordinator.hass, coordinator.update_pun, next_update_pun)
        _LOGGER.debug('Prossimo aggiornamento web: %s', next_update_pun.strftime('%d/%m/%Y %H:%M:%S'))

    if config.options[CONF_ACTUAL_DATA_ONLY] != coordinator.actual_data_only:
        # Modificata impostazione 'Usa dati reali'
        coordinator.actual_data_only = config.options[CONF_ACTUAL_DATA_ONLY]
        _LOGGER.debug('Nuovo valore \'usa dati reali\': %s.', coordinator.actual_data_only)

        # Forza un nuovo aggiornamento immediato
        coordinator.web_retries = 0
        await coordinator.update_pun()


class PUNDataUpdateCoordinator(DataUpdateCoordinator):
    session: ClientSession

    def __init__(self, hass: HomeAssistant, config: ConfigEntry) -> None:
        """Gestione dell'aggiornamento da Home Assistant"""
        super().__init__(
            hass,
            _LOGGER,
            # Nome dei dati (a fini di log)
            name = DOMAIN,
            # Nessun update_interval (aggiornamento automatico disattivato)
        )

        # Salva la sessione client e la configurazione
        self.session = async_get_clientsession(hass)

        # Inizializza i valori di configurazione (dalle opzioni o dalla configurazione iniziale)
        self.actual_data_only = config.options.get(CONF_ACTUAL_DATA_ONLY, config.data[CONF_ACTUAL_DATA_ONLY])
        self.scan_hour = config.options.get(CONF_SCAN_HOUR, config.data[CONF_SCAN_HOUR])

        # Inizializza i valori di default
        self.web_retries = 0
        self.web_last_run = datetime.min.replace(tzinfo=dt_util.UTC)
        self.pun = [0.0, 0.0, 0.0, 0.0]
        self.csud = [0.0, 0.0, 0.0, 0.0]
        self.orari = [0, 0, 0, 0]
        self.fascia_corrente = None
        _LOGGER.debug('Coordinator inizializzato (con \'usa dati reali\' = %s).', self.actual_data_only)

    async def _async_update_data(self):
        """Aggiornamento dati a intervalli prestabiliti"""
        
        # Calcola l'intervallo di date per il mese corrente
        date_end = dt_util.now().date()
        date_start = date(date_end.year, date_end.month, 1)

        # All'inizio del mese, aggiunge i valori del mese precedente
        # a meno che CONF_ACTUAL_DATA_ONLY non sia impostato
        if (not self.actual_data_only) and (date_end.day < 4):
            date_start = date_start - timedelta(days=3)

        # URL del sito Mercato elettrico
        LOGIN_URL = 'https://www.mercatoelettrico.org/It/Tools/Accessodati.aspx?ReturnUrl=%2fIt%2fdownload%2fDownloadDati.aspx%3fval%3dMGP_Prezzi&val=MGP_Prezzi'
        DOWNLOAD_URL = 'https://www.mercatoelettrico.org/It/download/DownloadDati.aspx?val=MGP_Prezzi'
        
        # Apre la pagina per generare i cookie e i campi nascosti
        async with self.session.get(LOGIN_URL) as response:
            soup = BeautifulSoup(await response.read(), features='html.parser')
        
        # Recupera i campi nascosti __VIEWSTATE e __EVENTVALIDATION per la prossima richiesta
        viewstate = soup.find('input',{'name':'__VIEWSTATE'})['value']
        eventvalidation = soup.find('input',{'name':'__EVENTVALIDATION'})['value']
        login_payload = {
            'ctl00$ContentPlaceHolder1$CBAccetto1': 'on',
            'ctl00$ContentPlaceHolder1$CBAccetto2': 'on',
            'ctl00$ContentPlaceHolder1$Button1': 'Accetto',
            '__VIEWSTATE': viewstate,
            '__EVENTVALIDATION': eventvalidation
        }

        # Effettua il login (che se corretto porta alla pagina di download XML grazie al 'ReturnUrl')
        async with self.session.post(LOGIN_URL, data=login_payload) as response:
            soup = BeautifulSoup(await response.read(), features='html.parser')

        # Recupera i campi nascosti __VIEWSTATE per la prossima richiesta
        viewstate = soup.find('input',{'name':'__VIEWSTATE'})['value']    
        data_request_payload = {
            'ctl00$ContentPlaceHolder1$tbDataStart': date_start.strftime('%d/%m/%Y'),
            'ctl00$ContentPlaceHolder1$tbDataStop': date_end.strftime('%d/%m/%Y'),
            'ctl00$ContentPlaceHolder1$btnScarica': 'scarica+file+xml+compresso',
            '__VIEWSTATE': viewstate
        }

        # Effettua il download dello ZIP con i file XML
        async with self.session.post(DOWNLOAD_URL, data=data_request_payload) as response:
            # Scompatta lo ZIP in memoria
            try:
                archive = zipfile.ZipFile(io.BytesIO(await response.read()))
            except:
                # Esce perché l'output non è uno ZIP
                raise UpdateFailed('Archivio ZIP scaricato dal sito non valido.')

        # Mostra i file nell'archivio
        _LOGGER.debug(f'{ len(archive.namelist()) } file trovati nell\'archivio (' + ', '.join(str(fn) for fn in archive.namelist()) + ').')

        # Carica le festività
        it_holidays = holidays.IT()

        # Inizializza le variabili di conteggio dei risultati
        mono = []
        f1 = []
        f2 = []
        f3 = []
        mono_csud = []
        f1_csud = []
        f2_csud = []
        f3_csud = []


        # Esamina ogni file XML nello ZIP (ordinandoli prima)
        for fn in sorted(archive.namelist()):
            # Scompatta il file XML in memoria
            xml_tree = et.parse(archive.open(fn))

            # Parsing dell'XML (1 file = 1 giorno)
            xml_root = xml_tree.getroot()

            # Estrae la data dal primo elemento (sarà identica per gli altri)
            dat_string = xml_root.find('Prezzi').find('Data').text #YYYYMMDD

            # Converte la stringa giorno in data
            dat_date = date(int(dat_string[0:4]), int(dat_string[4:6]), int(dat_string[6:8]))

            # Verifica la festività
            festivo = dat_date in it_holidays

            # Estrae le rimanenti informazioni
            for prezzi in xml_root.iter('Prezzi'):
                # Estrae l'ora dall'XML
                ora = int(prezzi.find('Ora').text) - 1 # 1..24
                
                # Estrae il prezzo PUN dall'XML in un float
                prezzo_string = prezzi.find('PUN').text
                prezzo_string = prezzo_string.replace('.','').replace(',','.')
                prezzo = float(prezzo_string) / 1000
                
                # Estrae il prezzo CSUD dall'XML in un float
                prezzo_string_csud = prezzi.find('CSUD').text
                prezzo_string_csud = prezzo_string_csud.replace('.','').replace(',','.')
                prezzo_csud = float(prezzo_string_csud) / 1000

                # Estrae la fascia oraria
                fascia = get_fascia_for_xml(dat_date, festivo, ora)

                # Calcola le statistiche
                mono.append(prezzo)
                mono_csud.append(prezzo_csud)
                if fascia == 3:
                    f3.append(prezzo)
                    f3_csud.append(prezzo_csud)
                elif fascia == 2:
                    f2.append(prezzo)
                    f2_csud.append(prezzo_csud)
                elif fascia == 1:
                    f1.append(prezzo)
                    f2_csud.append(prezzo_csud)

        # Salva i risultati nel coordinator
        self.orari[PUN_FASCIA_MONO] = len(mono)
        self.orari[PUN_FASCIA_F1] = len(f1)
        self.orari[PUN_FASCIA_F2] = len(f2)
        self.orari[PUN_FASCIA_F3] = len(f3)
        if self.orari[PUN_FASCIA_MONO] > 0:
            self.pun[PUN_FASCIA_MONO] = mean(mono)
            self.csud[PUN_FASCIA_MONO] = mean(mono_csud)
        if self.orari[PUN_FASCIA_F1] > 0:
            self.pun[PUN_FASCIA_F1] = mean(f1)
            self.csud[PUN_FASCIA_F1] = mean(f1_csud)
        if self.orari[PUN_FASCIA_F2] > 0:
            self.pun[PUN_FASCIA_F2] = mean(f2)
            self.csud[PUN_FASCIA_F2] = mean(f2_csud)
        if self.orari[PUN_FASCIA_F3] > 0:
            self.pun[PUN_FASCIA_F3] = mean(f3)
            self.csud[PUN_FASCIA_F3] = mean(f3_csud)
       
        # Logga i dati
        _LOGGER.debug('Numero di dati: ' + ', '.join(str(i) for i in self.orari))
        _LOGGER.debug('Valori PUN: ' + ', '.join(str(f) for f in self.pun))
        _LOGGER.debug('Valori CSUD: ' + ', '.join(str(f) for f in self.csud))
        return

    async def update_fascia(self, now=None):
        """Aggiorna la fascia oraria corrente"""

        # Ottiene la fascia oraria corrente e il prossimo aggiornamento
        self.fascia_corrente, next_update_fascia = get_fascia(dt_util.now())
        _LOGGER.info('Nuova fascia corrente: F%s (prossima: %s)', self.fascia_corrente, next_update_fascia.strftime('%a %d/%m/%Y %H:%M:%S'))

        # Notifica che i dati sono stati aggiornati (fascia)
        self.async_set_updated_data({ COORD_EVENT: EVENT_UPDATE_FASCIA })

        # Schedula la prossima esecuzione
        async_track_point_in_time(self.hass, self.update_fascia, next_update_fascia)

    async def update_pun(self, now=None):
        """Aggiorna i prezzi PUN da Internet (funziona solo se schedulata)"""

        # Evita rientranze nella funzione
        if ((dt_util.now() - self.web_last_run).total_seconds() < 2):
            return
        self.web_last_run = dt_util.now()

        # Verifica che non sia un nuovo tentativo dopo un errore
        if (self.web_retries == 0):
            # Verifica l'orario di esecuzione
            if (now is not None):
                if (now.date() != dt_util.now().date()):
                    # Esecuzione alla data non corretta (vecchia schedulazione)
                    _LOGGER.debug('Aggiornamento web ignorato a causa della data di schedulazione non corretta (%s).', now)
                    return
                elif (now.hour != self.scan_hour):
                    # Esecuzione all'ora non corretta (vecchia schedulazione)
                    _LOGGER.debug('Aggiornamento web ignorato a causa dell\'ora di schedulazione non corretta (%s != %s).', now.hour, self.scan_hour)
                    return
            elif (now is None):
                # Esecuzione non schedulata
                _LOGGER.debug('Esecuzione aggiornamento web non schedulato.')

        # Aggiorna i dati da web
        try:
            # Esegue l'aggiornamento
            await self._async_update_data()

            # Se non ci sono eccezioni, ha avuto successo
            self.web_retries = 0
        except:
            # Errori durante l'esecuzione dell'aggiornamento, riprova dopo
            if (self.web_retries == 0):
                # Primo errore
                self.web_retries = 4
                retry_in_minutes = 10
            elif (self.web_retries == 1):
                # Ultimo errore, tentativi esauriti
                self.web_retries = 0

                # Schedula al giorno dopo
                retry_in_minutes = 0
            else:
                # Ulteriori errori (4, 3, 2)
                self.web_retries -= 1
                retry_in_minutes = 60 * (4 - self.web_retries)
            
            # Prepara la schedulazione
            if (retry_in_minutes > 0):
                # Minuti dopo
                _LOGGER.warn('Errore durante l\'aggionamento via web, nuovo tentativo in %s minuti.', retry_in_minutes)
                next_update_pun = dt_util.utcnow() + timedelta(minutes=retry_in_minutes)
            else:
                # Giorno dopo
                _LOGGER.warn('Errore durante l\'aggionamento via web, tentativi esauriti.')
                next_update_pun = dt_util.now().replace(hour=self.scan_hour,
                                minute=0, second=0, microsecond=0) + timedelta(days=1)
                _LOGGER.debug('Prossimo aggiornamento web: %s', next_update_pun.strftime('%d/%m/%Y %H:%M:%S'))
            
            # Schedula ed esce
            async_track_point_in_time(self.hass, self.update_pun, next_update_pun)
            return

        # Notifica che i dati PUN sono stati aggiornati con successo
        self.async_set_updated_data({ COORD_EVENT: EVENT_UPDATE_PUN })

        # Calcola la data della prossima esecuzione
        next_update_pun = dt_util.now().replace(hour=self.scan_hour,
                                minute=0, second=0, microsecond=0)
        if next_update_pun <= dt_util.now():
            # Se l'evento è già trascorso la esegue domani alla stessa ora
            next_update_pun = next_update_pun + timedelta(days=1)

        # Schedula la prossima esecuzione
        async_track_point_in_time(self.hass, self.update_pun, next_update_pun)
        _LOGGER.debug('Prossimo aggiornamento web: %s', next_update_pun.strftime('%d/%m/%Y %H:%M:%S'))

def get_fascia_for_xml(data, festivo, ora) -> int:
    """Restituisce il numero di fascia oraria di un determinato giorno/ora"""
    #F1 = lu-ve 8-19
    #F2 = lu-ve 7-8, lu-ve 19-23, sa 7-23
    #F3 = lu-sa 0-7, lu-sa 23-24, do, festivi
    if festivo or (data.weekday() == 6):
        # Festivi e domeniche
        return 3
    elif (data.weekday() == 5):
        # Sabato
        if (ora >= 7) and (ora < 23):
            return 2
        else:
            return 3
    else:
        # Altri giorni della settimana
        if (ora == 7) or ((ora >= 19) and (ora < 23)):
            return 2
        elif (ora == 23) or ((ora >= 0) and (ora < 7)):
            return 3
    return 1

def get_fascia(dataora: datetime) -> Tuple[int, datetime]:
    """Restituisce la fascia della data/ora indicata (o quella corrente) e la data del prossimo cambiamento"""

    # Verifica se la data corrente è un giorno con festività
    festivo = dataora in holidays.IT()
    
    # Identifica la fascia corrente
    # F1 = lu-ve 8-19
    # F2 = lu-ve 7-8, lu-ve 19-23, sa 7-23
    # F3 = lu-sa 0-7, lu-sa 23-24, do, festivi
    if festivo or (dataora.weekday() == 6):
        # Festivi e domeniche
        fascia = 3

        # Prossima fascia: alle 7 di un giorno non domenica o festività
        prossima = (dataora + timedelta(days=1)).replace(hour=7,
                        minute=0, second=0, microsecond=0)
        while ((prossima in holidays.IT()) or (prossima.weekday() == 6)):
            prossima += timedelta(days=1)

    elif (dataora.weekday() == 5):
        # Sabato
        if (dataora.hour >= 7) and (dataora.hour < 23):
            # Sabato dalle 7 alle 23
            fascia = 2

            # Prossima fascia: alle 23 dello stesso giorno
            prossima = dataora.replace(hour=23,
                            minute=0, second=0, microsecond=0)
        else:
            # Sabato dopo le 23
            fascia = 3

            # Prossima fascia: alle 7 di un giorno non domenica o festività
            prossima = (dataora + timedelta(days=1)).replace(hour=7,
                        minute=0, second=0, microsecond=0)
            while ((prossima in holidays.IT()) or (prossima.weekday() == 6)):
                prossima += timedelta(days=1)
    else:
        # Altri giorni della settimana
        if (dataora.hour == 7):
            # Lunedì-venerdì dalle 7 alle 8
            fascia = 2

            # Prossima fascia: alle 8 dello stesso giorno
            prossima = dataora.replace(hour=8,
                            minute=0, second=0, microsecond=0)

        elif ((dataora.hour >= 19) and (dataora.hour < 23)):
            # Lunedì-venerdì dalle 19 alle 23
            fascia = 2

            # Prossima fascia: alle 23 dello stesso giorno
            prossima = dataora.replace(hour=23,
                            minute=0, second=0, microsecond=0)

        elif ((dataora.hour == 23) or ((dataora.hour >= 0) and (dataora.hour < 7))):
            # Lunedì-venerdì dalle 23 alle 24 e dalle 0 alle 7
            fascia = 3

            # Prossima fascia: alle 7 di un giorno non domenica o festività
            prossima = (dataora + timedelta(days=1)).replace(hour=7,
                        minute=0, second=0, microsecond=0)
            while ((prossima in holidays.IT()) or (prossima.weekday() == 6)):
                prossima += timedelta(days=1)

        else:
            # Lunedì-venerdì dalle 8 alle 19
            fascia = 1

            # Prossima fascia: alle 19 dello stesso giorno
            prossima = dataora.replace(hour=19,
                            minute=0, second=0, microsecond=0)
    
    return fascia, prossima
