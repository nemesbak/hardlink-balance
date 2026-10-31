#!/bin/bash
# Parallel copies: 2 full source disks, 2 empty destinations, XFS loop disks in RAM.
# Checks: real concurrency, never two copies on one disk, links + content kept, stop mid-run is clean.
set -u
ENG="$(cd "$(dirname "$0")/.." && pwd)/scripts/hardlink_balance.py"
R=/tmp/hlbp; FAIL=0
ok(){ echo "  OK   $*"; }; ko(){ echo "  FALLO $*"; FAIL=1; }
cleanup(){ pkill -f hardlink_balance_slowtest.py 2>/dev/null; for d in 94 95 96 97; do umount $R/disk$d 2>/dev/null; done; rm -rf $R /tmp/hlbpimg /tmp/hardlink_balance_slowtest.py /tmp/hlbp.cfg; }
cleanup; mkdir -p /tmp/hlbpimg
for d in 94 95 96 97; do truncate -s 400M /tmp/hlbpimg/d$d.img; mkfs.xfs -q /tmp/hlbpimg/d$d.img; mkdir -p $R/disk$d; mount -o loop /tmp/hlbpimg/d$d.img $R/disk$d; done
echo 'RUNS_DIR="/tmp/hlbp_runs"' > /tmp/hlbp.cfg
mk(){ mkdir -p "$(dirname "$1")"; head -c ${2}M /dev/urandom > "$1"; }
# Each source: 6 seasons, each 2 episodes hardlinked torrent<->library
for s in 94 95; do for n in 1 2 3 4 5 6; do for e in 1 2; do
  T=$R/disk$s/data/torrents/S$s.$n/e$e.mkv; L="$R/disk$s/data/media/Show$s/Season $n/e$e.mkv"
  mk "$T" 12; mkdir -p "$(dirname "$L")"; ln "$T" "$L"
done; done; done
md5sum $R/disk9[45]/data/media/*/*/*.mkv | sed "s#$R/disk9[0-9]/##" | sort -k2 > /tmp/hlbp_before.md5
# Slow copy (5 MB/s) so the run lasts long enough to observe it
sed 's/\["rsync", "-aX"/["rsync", "-aX", "--bwlimit=5000"/' "$ENG" > /tmp/hardlink_balance_slowtest.py
RUN="python3 /tmp/hardlink_balance_slowtest.py --cfg-file /tmp/hlbp.cfg --mnt-root $R --headroom-mb 5 --min-size-kb 0 --min-age-min 0 --tolerance 3"

echo "== 6) dos copias a la vez, nunca dos en el mismo disco"
$RUN --execute --parallel 3 > /tmp/hlbp.out 2>&1 & P=$!
MAXC=0; CLASH=0; GOWN=""
while kill -0 $P 2>/dev/null; do
  [ -z "$GOWN" ] && [ "$(pgrep -a rsync | grep -c bwlimit=5000)" -gt 0 ] && GOWN=$(python3 "$ENG" --mnt-root $R --guards)
  # local rsync forks 2-3 processes per copy: one copy = one distinct command line
  mapfile -t CMDS < <(pgrep -a rsync | grep bwlimit=5000 | cut -d" " -f2- | sort -u)
  C=${#CMDS[@]}; [ "$C" -gt "$MAXC" ] && MAXC=$C
  mapfile -t L < <(printf "%s
" "${CMDS[@]}" | grep -o "$R/disk9[0-9]" )
  D=$(printf '%s\n' "${L[@]}" | sort | uniq -d | wc -l); [ "$D" -gt 0 ] && CLASH=1
  sleep 0.2
done
wait $P
grep -E "EXECUTING|DONE|STOPPED" /tmp/hlbp.out
[ "$MAXC" -ge 2 ] && ok "copias simultáneas: hasta $MAXC" || ko "no hubo copias en paralelo (máx $MAXC)"
[ "$CLASH" = 0 ] && ok "ningún disco en dos copias a la vez" || ko "un disco participó en dos copias a la vez"
md5sum $R/disk9*/data/media/*/*/*.mkv | sed "s#$R/disk9[0-9]/##" | sort -k2 > /tmp/hlbp_after.md5
diff -q /tmp/hlbp_before.md5 /tmp/hlbp_after.md5 >/dev/null && ok "los 24 episodios existen una sola vez con el mismo contenido" || ko "contenido o duplicados distintos"
BAD=0; MOVED=0
for L in $R/disk9*/data/media/*/*/*.mkv; do
  d=${L%%/data/*}; rel=${L#*/media/}; show=${rel%%/*}; s=${show#Show}; seas=$(echo "$rel" | sed 's#.*/Season \([0-9]*\)/.*#\1#'); e=$(basename "$L")
  T=$d/data/torrents/S$s.$seas/$e
  [ "$(stat -c %h "$L")" = 2 ] && [ "$(stat -c %i "$L")" = "$(stat -c %i "$T" 2>/dev/null)" ] || BAD=$((BAD+1))
  case $d in *96|*97) MOVED=$((MOVED+1));; esac
done
[ "$BAD" = 0 ] && ok "todos los pares torrent<->biblioteca siguen enlazados (nlink=2, mismo inodo)" || ko "$BAD hardlinks rotos"
[ "$MOVED" -gt 0 ] && ok "$MOVED episodios movidos a los discos destino" || ko "no se movió nada"
find $R -name '*.hlb-tmp' | grep -q . && ko "quedan temporales" || ok "sin temporales"
grep -q "ERROR" /tmp/hlbp.out && ko "hay ERROR en el log" || ok "sin errores en el log"
grep -q "Halting" /tmp/hlbp.out && ko "el motor se detuvo solo: $(grep Halting /tmp/hlbp.out)" || ok "sus propias copias no lo detienen"
echo "$GOWN" | grep -q '"rsync": ""' && ok "--guards no cuenta las copias del propio motor" || ko "--guards ve sus copias como ajenas: $GOWN"
df -h $R/disk9* | tail -4

echo "== 7) parar a mitad con varias copias en marcha"
for s in 94 95; do for n in 7 8; do mk "$R/disk$s/data/media/Extra$s/Season $n/x.mkv" 30; done; done
$RUN --execute --parallel 3 --tolerance 1 > /tmp/hlbp7.out 2>&1 & P=$!
cnt(){ pgrep -a rsync | grep bwlimit=5000 | cut -d" " -f2- | sort -u | wc -l; }
for i in $(seq 1 50); do [ "$(cnt)" -ge 2 ] && break; sleep 0.2; done
NOW=$(cnt)
python3 "$ENG" --stop >/dev/null; wait $P; sleep 0.5
echo "  (copias en marcha al parar: $NOW)"
pgrep -f bwlimit=5000 >/dev/null && ko "quedan rsync vivos" || ok "todas las copias detenidas"
find $R -name '*.hlb-tmp' | grep -q . && ko "quedan temporales tras parar" || ok "temporales borrados al parar"
N=$(ls $R/disk9[45]/data/media/Extra*/*/x.mkv 2>/dev/null | wc -l); M=$(ls $R/disk9[67]/data/media/Extra*/*/x.mkv 2>/dev/null | wc -l)
[ $((N+M)) = 4 ] && ok "los 4 archivos existen exactamente una vez ($N en origen, $M movidos)" || ko "archivos: $N en origen + $M en destino (deberían ser 4)"
grep -q "STOPPED" /tmp/hlbp7.out && ok "la pasada termina como parada" || ko "no consta la parada"
[ -f /tmp/hlbp_runs/run_*.json ] 2>/dev/null; ls /tmp/hlbp_runs/*.json >/dev/null 2>&1 && ok "informe guardado en RUNS_DIR de prueba" || ko "sin informe"
rm -rf /tmp/hlbp_runs
echo "== 8) otra copia ajena entre discos: no arranca"
mk "$R/disk94/data/media/Ajeno/a.mkv" 40
rsync -a --bwlimit=2000 "$R/disk94/data/media/Ajeno/a.mkv" "$R/disk96/ajeno.tmp" & RS=$!
sleep 0.5
python3 "$ENG" --cfg-file /tmp/hlbp.cfg --mnt-root $R --headroom-mb 5 --min-size-kb 0 --min-age-min 0 --tolerance 1 --execute > /tmp/hlbp8.out 2>&1
kill $RS 2>/dev/null; wait $RS 2>/dev/null
grep -q "ABORT: another rsync" /tmp/hlbp8.out && ok "se niega a mover mientras corre un rsync ajeno" || ko "no detectó el rsync ajeno: $(tail -2 /tmp/hlbp8.out)"
cleanup
[ $FAIL = 0 ] && echo "TODAS LAS PRUEBAS OK" || echo "HAY FALLOS"
