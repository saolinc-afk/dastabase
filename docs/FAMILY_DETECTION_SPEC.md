# Family Business Detection Specification

**Projekt:** Dastabase\
**Status:** Delovni dokument (living document)

------------------------------------------------------------------------

# Namen

Ta dokument določa **poslovna pravila** za prepoznavanje družinskih
podjetij v projektu Dastabase.

To ni tehnična dokumentacija, ampak dokument za usklajevanje pravil med
člani ekipe.

Python koda (`family.py`) mora slediti pravilom iz tega dokumenta.

Če se pravila spremenijo, najprej posodobimo ta dokument, nato
konfiguracijo (`family_rules.yaml`) in šele nato programsko kodo.

------------------------------------------------------------------------

# Cilj

Želimo čim bolj zanesljivo prepoznati podjetja, za katera obstajajo
javni dokazi, da so družinska.

Sistem **nikoli ne sklepa, da podjetje ni družinsko**.

Možni rezultati so:

-   **EXPLICIT** -- podjetje samo zase jasno pove, da je družinsko.
-   **LIKELY** -- obstaja več močnih posrednih dokazov.
-   **UNKNOWN** -- ni dovolj dokazov.

------------------------------------------------------------------------

# Temeljna načela

1.  Nikoli ne uporabljamo rezultata **NO**.
2.  Vsak pozitiven rezultat mora biti razložljiv.
3.  Vsak dokaz se shrani.
4.  Pravila morajo biti razumljiva tudi ljudem, ki niso programerji.
5.  Najprej uporabljamo deterministična pravila. AI je lahko kasneje
    dodatna validacija.

------------------------------------------------------------------------

# Katere strani pregledamo

Vedno:

-   domača stran (/)

Če obstajajo:

-   O nas
-   O podjetju
-   About
-   About us
-   Company
-   History
-   Zgodovina

Kasneje lahko dodamo:

-   Vodstvo
-   Team
-   Management

------------------------------------------------------------------------

# Katere izraze bomo iskali

Spodnji seznam predstavlja začetni nabor izrazov. Namenjen je
dopolnjevanju skozi čas.

## A. Neposredna izjava (najmočnejši dokaz)

Primeri:

-   družinsko podjetje
-   družinsko vodeno podjetje
-   family business
-   family-owned
-   family owned
-   family-run

Predlog uteži: **100**

------------------------------------------------------------------------

## B. Generacije

Primeri:

-   druga generacija
-   tretja generacija
-   second generation
-   third generation

Predlog uteži: **70**

------------------------------------------------------------------------

## C. Ustanovitelj

Primeri:

-   ustanovil
-   ustanovila
-   founded by
-   our founder

To samo po sebi še ni dovolj za pozitiven rezultat.

Predlog uteži: **30**

------------------------------------------------------------------------

## D. Družina in tradicija

Primeri:

-   naša družina
-   družinska tradicija
-   our family
-   family tradition

Predlog uteži: **50**

------------------------------------------------------------------------

## E. Prihodnje izboljšave

Možni dodatni indikatorji:

-   ujemanje priimkov ustanovitelja in vodstva,
-   omembe več družinskih članov,
-   zgodovina podjetja.

------------------------------------------------------------------------

# Kaj želimo shraniti

Ob vsakem pozitivnem zadetku želimo shraniti:

-   stran, kjer je bil dokaz najden,
-   vrsto dokaza,
-   dejanski najdeni tekst,
-   oceno zaupanja (confidence).

Primer:

``` json
[
  {
    "page": "/o-nas",
    "type": "explicit",
    "text": "Smo družinsko podjetje že od leta 1988."
  }
]
```

------------------------------------------------------------------------

# Česa NE uporabljamo kot dokaz

Naslednji podatki sami po sebi niso dovolj:

-   pravna oblika,
-   občina,
-   naslov,
-   prihodki,
-   število zaposlenih,
-   starost podjetja.

------------------------------------------------------------------------

# Odprta vprašanja

Prosimo sodelavce za komentarje:

1.  Katere ključne besede še manjkajo?
2.  Katere izraze bi odstranili?
3.  Ali naj bo status **LIKELY** prikazan uporabnikom ali samo interno?
4.  Katere dodatne strani naj sistem pregleda?
5.  Katere primere družinskih podjetij poznate, kjer zgornja pravila ne
    bi zadostovala?

------------------------------------------------------------------------

# Decision Log

## 2026-07-09

-   Family detection temelji na dokazih.
-   Rezultat **NO** ne obstaja.
-   Uporabljamo statuse EXPLICIT / LIKELY / UNKNOWN.
-   Shranjujemo dokaze (evidence).
-   Poslovna pravila živijo v tem dokumentu, ne v Python kodi.
