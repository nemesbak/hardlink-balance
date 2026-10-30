#!/bin/bash
# Pruebas de hardlink_balance.py con 3 discos XFS falsos en RAM
set -u
PY="python3 $(dirname "$0")/../scripts/hardlink_balance.py --mnt-root /tmp/hlbt --headroom-mb 5"
R=/tmp/hlbt; FAIL=0; HOLD=
ok(){ echo "  OK   $*"; }; ko(){ echo "  FALLO $*"; FAIL=1; }
cleanup(){ kill $HOLD 2>/dev/null; for d in 91 92 93; do umount $R/disk$d 2>/dev/null; done; rm -rf $R /tmp/hlbimg; }
cleanup; mkdir -p /tmp/hlbimg
for d in 91 92 93; do truncate -s 320M /tmp/hlbimg/d$d.img; mkfs.xfs -q /tmp/hlbimg/d$d.img; mkdir -p $R/disk$d; mount -o loop /tmp/hlbimg/d$d.img $R/disk$d; done
D=$R/disk91/datos
mk(){ mkdir -p "$(dirname "$1")"; head -c ${2}M /dev/urandom > "$1"; }
# A: temporada con pack de torrent enlazado + nfo pequeño
for e in 1 2 3; do mk "$D/torrents/tv/Serie.S01/e$e.mkv" 25; mkdir -p "$D/media/series/Serie/Season 01"; ln "$D/torrents/tv/Serie.S01/e$e.mkv" "$D/media/series/Serie/Season 01/e$e.mkv"; done
echo nfo > "$D/media/series/Serie/Season 01/tvshow.nfo"
# Película sin enlaces
mk "$D/media/peliculas/Peli (2020)/peli.mkv" 40
# B: enlace extra en carpeta oculta de la raíz
mk "$D/media/peliculas/Oculta/o.mkv" 30; mkdir -p $R/disk91/.oculto; ln "$D/media/peliculas/Oculta/o.mkv" $R/disk91/.oculto/o.mkv
# C: descarga parcial
mk "$D/torrents/tv/Bajando/x.mkv.!qB" 20
# C2: abierto para escritura (qBit escribiendo)
mk "$D/torrents/tv/Abierto/y.mkv" 20; sleep 600 3>>"$D/torrents/tv/Abierto/y.mkv" & HOLD=$!
# D: colisión en destino (en los dos discos destino)
mk "$D/media/peliculas/Choque/c.mkv" 30; for d in 92 93; do mkdir -p $R/disk$d/datos/media/peliculas/Choque; echo otro > $R/disk$d/datos/media/peliculas/Choque/c.mkv; done
# Temporal huérfano de un fallo anterior
echo basura > "$D/media/peliculas/Peli (2020)/.peli.mkv.hlb-tmp"
INO_A=$(stat -c %i "$D/media/series/Serie/Season 01/e1.mkv"); SUM_A=$(md5sum < "$D/media/series/Serie/Season 01/e1.mkv")
df -h $R/disk9* | tail -3
echo "== 1) dry-run con antigüedad 60 min (todo es nuevo: no debe planear nada)"
rm -f /var/log/hardlink_balance.log
$PY --dry-run >/dev/null; grep -q "skipped {'recent'" /var/log/hardlink_balance.log && ok "carpetas recientes saltadas" || ko "recent"
echo "== 2) ejecución real con --min-age-min 0"
$PY --execute --min-age-min 0 --tolerance 2 > /tmp/hlbt.out; tail -n 25 /tmp/hlbt.out
F=$(ls $R/disk9[23]/datos/media/series/Serie/"Season 01"/e1.mkv 2>/dev/null)
[ -n "$F" ] && ok "temporada movida a $F" || ko "temporada no movida"
if [ -n "$F" ]; then N=${F%%/datos/*}
  [ "$(stat -c %h "$F")" = 2 ] && [ "$(stat -c %i "$F")" = "$(stat -c %i "$N/datos/torrents/tv/Serie.S01/e1.mkv")" ] && ok "hardlink torrent<->biblioteca conservado (nlink=2)" || ko "hardlink roto"
  [ "$(md5sum < "$F")" = "$SUM_A" ] && ok "contenido idéntico" || ko "md5"
  [ -f "$(dirname "$F")/tvshow.nfo" ] && ok "nfo viaja con la carpeta" || ko "nfo"
  [ "$(stat -c %U:%G "$(dirname "$F")")" = "nobody:users" ] && ok "carpeta nueva nobody:users" || ko "permisos"
  ls "$D/media/series/Serie" "$D/torrents/tv/Serie.S01" >/dev/null 2>&1 && ko "quedan carpetas vacías en origen" || ok "origen limpio, sin carpetas vacías"
fi
[ -f $R/disk91/.oculto/o.mkv ] && [ -f "$D/media/peliculas/Oculta/o.mkv" ] && [ "$(stat -c %h $R/disk91/.oculto/o.mkv)" = 2 ] && ok "grupo con enlace oculto intacto" || ko "enlace oculto"
[ -f "$D/torrents/tv/Bajando/x.mkv.!qB" ] && ok "parcial .!qB intacto" || ko "parcial"
[ -f "$D/torrents/tv/Abierto/y.mkv" ] && ! ls $R/disk9[23]/datos/torrents/tv/Abierto 2>/dev/null && ok "archivo abierto en escritura intacto" || ko "abierto"
[ -f "$D/media/peliculas/Choque/c.mkv" ] && grep -q otro $R/disk92/datos/media/peliculas/Choque/c.mkv && ok "colisión: nada sobrescrito y no planificado" || ko "colisión"
ls -a "$D/media/peliculas/Peli (2020)" $R/disk9[23]/datos/media/peliculas/*/ 2>/dev/null | grep -q hlb-tmp && ko "quedan .hlb-tmp" || ok "sin temporales"
find $R -name '*.hlb-tmp' | grep -q . && ko "tmp" ; true
echo "== 3) parada a mitad de copia (rsync lento con bwlimit)"
mk "$R/disk91/datos/media/peliculas/Lenta/l.mkv" 25
sed 's/\["rsync", "-aX"/["rsync", "-aX", "--bwlimit=5000"/' $(dirname "$0")/../scripts/hardlink_balance.py > /tmp/hb_slow.py
python3 /tmp/hb_slow.py --mnt-root $R --headroom-mb 5 --execute --min-age-min 0 --tolerance 1 >/dev/null & P=$!
sleep 2; python3 $(dirname "$0")/../scripts/hardlink_balance.py --stop >/dev/null; wait $P
[ -f "$R/disk91/datos/media/peliculas/Lenta/l.mkv" ] && ok "origen intacto tras parar" || ko "origen tras parar"
find $R -name '*.hlb-tmp' | grep -q . && ko "quedó temporal tras parar" || ok "temporal borrado al parar"
sleep 1; pgrep -x rsync -a | grep -q bwlimit=5000 >/dev/null && ko "rsync sigue vivo" || ok "rsync detenido"
grep -q "ABORTED copy of datos/media/peliculas/Lenta" /var/log/hardlink_balance.log && ok "copia en curso abortada (log: ABORTED)" || ko "no se llegó a copiar Lenta"
df -h $R/disk9* | tail -3
echo "== 4) shareExclude respetado + temporales huérfanos en destino"
mkdir -p /tmp/hlbshares; echo 'shareExclude="disk93"' > /tmp/hlbshares/solo92.cfg
rm -rf $R/disk91/datos/media/peliculas/Lenta; mk "$R/disk91/solo92/peli/s.mkv" 20
echo basura > $R/disk93/.huerfano.mkv.hlb-tmp
mkdir -p $R/disk93/datos/x; echo basura > $R/disk93/datos/x/.h.mkv.hlb-tmp
python3 $(dirname "$0")/../scripts/hardlink_balance.py --mnt-root $R --headroom-mb 5 --shares-cfg-dir /tmp/hlbshares --execute --min-age-min 0 --tolerance 10 > /tmp/hlbt4.out
grep -E "MOVED|Target|disk9" /tmp/hlbt4.out | tail -8
[ -f $R/disk92/solo92/peli/s.mkv ] && ok "share movido a disk92 (disk93 excluido)" || { [ -f $R/disk93/solo92/peli/s.mkv ] && ko "share puesto en disco excluido" || ko "share no movido"; }
[ -e $R/disk93/datos/x/.h.mkv.hlb-tmp ] && ko "temporal huérfano en destino sigue" || ok "temporal huérfano en destino borrado"
rm -rf /tmp/hlbshares
df -h $R/disk9* | tail -3
cleanup
[ $FAIL = 0 ] && echo "TODAS LAS PRUEBAS OK" || echo "HAY FALLOS"
