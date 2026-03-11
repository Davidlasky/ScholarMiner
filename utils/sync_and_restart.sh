#!/bin/bash
set -e

PROJECT="aerial-citron-428307-c5"
ZONE="us-west1-a"
INSTANCE="ieee-search-cluster-m"
REMOTE_BASE="/opt/ieee-search-engine"
G_OPTS="--project=$PROJECT --zone=$ZONE --quiet"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
LOCAL_APP_DIR="$PROJECT_ROOT/cluster-app"

echo "Step 1: Creating remote helper script..."
cat << 'EOF' > /tmp/remote_setup.sh
#!/bin/bash
REMOTE_BASE="/opt/ieee-search-engine"
pip3 install --user kafka-python-ng requests beautifulsoup4 psycopg2-binary
sudo mkdir -p $REMOTE_BASE
sudo chown -R $USER:$USER $REMOTE_BASE
cp -r /tmp/cluster-app/* $REMOTE_BASE/
rm -rf /tmp/cluster-app
cd $REMOTE_BASE
sudo pkill -f backend.py || true
touch backend.log
chmod 666 backend.log
nohup python3 -u backend.py >> backend.log 2>&1 &

sleep 2
echo "Backend environment verified and service started."
EOF

echo "Step 2: Syncing code and helper script..."
gcloud compute scp --recurse "$LOCAL_APP_DIR" /tmp/remote_setup.sh $INSTANCE:/tmp/ $G_OPTS

echo "Step 3: Running setup on Master..."
gcloud compute ssh $INSTANCE $G_OPTS --command="bash /tmp/remote_setup.sh"

echo "Step 4: Tailing logs..."
gcloud compute ssh $INSTANCE $G_OPTS --command="tail -f $REMOTE_BASE/backend.log"