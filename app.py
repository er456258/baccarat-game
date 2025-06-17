import eventlet
eventlet.monkey_patch() # Must be called very early

from flask import Flask, render_template, request, jsonify, redirect, url_for
from flask_socketio import SocketIO, emit
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from datetime import datetime
import random
import json
import time
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///baccarat.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
socketio = SocketIO(app, 
                   cors_allowed_origins="*",
                   async_mode='eventlet',
                   ping_timeout=60,
                   ping_interval=25,
                   logger=True,
                   engineio_logger=True)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# 用戶模型
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(120), nullable=False)
    balance = db.Column(db.Integer, default=10000)
    total_winnings = db.Column(db.Integer, default=0)

# 遊戲狀態
game_state = {
    'phase': 'waiting',  # waiting, betting, dealing, result
    'time_left': 20,
    'banker_cards': [],
    'player_cards': [],
    'deck': [],
    'bets': {},
    'results': None,
    'round_number': 0,
    'player_current_score': 0,
    'banker_current_score': 0,
    'winner': None
}

# 賠率設定
PAYOUT_RATES = {
    'banker': 0.95,  # 莊贏賠率 0.95
    'player': 1.0,   # 閒贏賠率 1.0
    'tie': 8.0,      # 和局賠率 8.0
    'banker_pair': 11.0,  # 莊對子賠率 11.0
    'player_pair': 11.0,  # 閒對子賠率 11.0
    'lucky_six_2': 12.0,  # 幸運六（兩張牌）賠率 12.0
    'lucky_six_3': 20.0   # 幸運六（三張牌）賠率 20.0
}

# 用於追蹤用戶的 session ID
connected_users_sids = {}

def calculate_score(cards):
    """計算牌面點數"""
    score = 0
    for card in cards:
        if card['value'] == 'A':
            score += 1
        elif card['value'] in ['J', 'Q', 'K', '10']:
            score += 0 # Ten-value cards count as 0
        else:
            score += int(card['value'])
    return score % 10

def should_draw_third_card(player_score, banker_score, player_third_card=None):
    """判斷是否需要補牌"""
    # 如果任一方有8或9點，不需要補牌
    if player_score >= 8 or banker_score >= 8:
        return False, False

    # 閒家補牌規則
    player_draw = player_score <= 5

    # 莊家補牌規則
    if not player_draw:
        # 如果閒家不補牌，莊家按照閒家規則補牌
        banker_draw = banker_score <= 5
    else:
        # 如果閒家補牌，莊家按照特殊規則補牌
        if banker_score <= 2:
            banker_draw = True
        elif banker_score == 3:
            banker_draw = player_third_card and calculate_score([player_third_card]) != 8
        elif banker_score == 4:
            banker_draw = player_third_card and calculate_score([player_third_card]) not in [0, 1, 8, 9]
        elif banker_score == 5:
            banker_draw = player_third_card and calculate_score([player_third_card]) not in [0, 1, 2, 3, 8, 9]
        elif banker_score == 6:
            banker_draw = player_third_card and calculate_score([player_third_card]) in [6, 7]
        else:
            banker_draw = False

    return player_draw, banker_draw

def determine_winner(banker_score, player_score):
    """判斷勝負"""
    if banker_score > player_score:
        return 'banker'
    elif player_score > banker_score:
        return 'player'
    else:
        return 'tie'

def calculate_payout(bet_type, amount, winner, banker_cards, player_cards):
    """計算派彩"""
    payout = 0
    
    # 檢查對子
    banker_pair = len(banker_cards) >= 2 and banker_cards[0]['value'] == banker_cards[1]['value']
    player_pair = len(player_cards) >= 2 and player_cards[0]['value'] == player_cards[1]['value']
    
    # 檢查幸運六
    banker_score = calculate_score(banker_cards)
    player_score = calculate_score(player_cards)
    banker_lucky_six = banker_score == 6 and winner == 'banker'
    player_lucky_six = player_score == 6 and winner == 'player'
    
    # 計算基本派彩
    if bet_type == winner:
        if bet_type == 'banker':
            payout = amount * (1 + PAYOUT_RATES['banker'])
        elif bet_type == 'player':
            payout = amount * (1 + PAYOUT_RATES['player'])
        elif bet_type == 'tie':
            payout = amount * (1 + PAYOUT_RATES['tie'])
    
    # 計算對子派彩
    if bet_type == 'banker_pair' and banker_pair:
        payout += amount * (1 + PAYOUT_RATES['banker_pair'])
    if bet_type == 'player_pair' and player_pair:
        payout += amount * (1 + PAYOUT_RATES['player_pair'])
    
    # 計算幸運六派彩
    if bet_type == 'lucky_six':
        if banker_lucky_six:
            if len(banker_cards) == 2:
                payout += amount * (1 + PAYOUT_RATES['lucky_six_2'])
            else:
                payout += amount * (1 + PAYOUT_RATES['lucky_six_3'])
        if player_lucky_six:
            if len(player_cards) == 2:
                payout += amount * (1 + PAYOUT_RATES['lucky_six_2'])
            else:
                payout += amount * (1 + PAYOUT_RATES['lucky_six_3'])
    
    return payout

def game_loop():
    """遊戲主循環"""
    print("Starting game loop...")
    with app.app_context():
        while True:
            try:
                # 開始新一局
                print("Starting new round...")
                game_state['phase'] = 'betting'
                game_state['time_left'] = 20
                game_state['round_number'] += 1
                game_state['banker_cards'] = []
                game_state['player_cards'] = []
                game_state['deck'] = init_deck()
                game_state['bets'] = {}
                game_state['results'] = None
                game_state['player_current_score'] = 0
                game_state['banker_current_score'] = 0
                game_state['winner'] = None

                # 通知所有玩家新一局開始
                try:
                    socketio.emit('game_state', {
                        'phase': 'betting',
                        'time_left': 20,
                        'round_number': game_state['round_number'],
                        'banker_current_score': 0,
                        'player_current_score': 0
                    })
                    print(f"Emitted game state for new round {game_state['round_number']}")
                except Exception as e:
                    print(f"Error emitting new round state: {str(e)}")

                # 下注階段
                for i in range(20, 0, -1):
                    game_state['time_left'] = i
                    try:
                        socketio.emit('game_state', {
                            'phase': 'betting',
                            'time_left': i,
                            'banker_current_score': game_state['banker_current_score'],
                            'player_current_score': game_state['player_current_score']
                        })
                        print(f"Betting phase - Time left: {i}")
                        eventlet.sleep(1)
                    except Exception as e:
                        print(f"Error during betting phase: {str(e)}")
                        continue

                # 發牌階段
                game_state['phase'] = 'dealing'
                socketio.emit('game_state', {'phase': 'dealing'})
                
                # 發前兩張牌
                for _ in range(2):
                    game_state['player_cards'].append(game_state['deck'].pop())
                    game_state['banker_cards'].append(game_state['deck'].pop())
                
                game_state['player_current_score'] = calculate_score(game_state['player_cards'])
                game_state['banker_current_score'] = calculate_score(game_state['banker_cards'])
                
                socketio.emit('game_state', {
                    'phase': 'dealing',
                    'banker_current_score': game_state['banker_current_score'],
                    'player_current_score': game_state['player_current_score']
                })
                
                eventlet.sleep(2)
                
                # 判斷是否需要補牌
                player_draw, banker_draw = should_draw_third_card(
                    game_state['player_current_score'],
                    game_state['banker_current_score']
                )
                
                if player_draw:
                    game_state['player_cards'].append(game_state['deck'].pop())
                    game_state['player_current_score'] = calculate_score(game_state['player_cards'])
                    socketio.emit('game_state', {
                        'phase': 'dealing',
                        'player_current_score': game_state['player_current_score']
                    })
                    eventlet.sleep(1)
                
                if banker_draw:
                    game_state['banker_cards'].append(game_state['deck'].pop())
                    game_state['banker_current_score'] = calculate_score(game_state['banker_cards'])
                    socketio.emit('game_state', {
                        'phase': 'dealing',
                        'banker_current_score': game_state['banker_current_score']
                    })
                    eventlet.sleep(1)
                
                # 計算結果
                game_state['winner'] = determine_winner(
                    game_state['banker_current_score'],
                    game_state['player_current_score']
                )
                
                # 結算階段
                game_state['phase'] = 'result'
                socketio.emit('game_state', {
                    'phase': 'result',
                    'winner': game_state['winner'],
                    'banker_current_score': game_state['banker_current_score'],
                    'player_current_score': game_state['player_current_score']
                })
                
                # 處理派彩
                for user_id, bets in game_state['bets'].items():
                    total_payout = 0
                    for bet_type, amount in bets.items():
                        payout = calculate_payout(
                            bet_type,
                            amount,
                            game_state['winner'],
                            game_state['banker_cards'],
                            game_state['player_cards']
                        )
                        total_payout += payout
                    
                    if total_payout > 0:
                        user = User.query.get(user_id)
                        if user:
                            user.balance += total_payout
                            user.total_winnings += (total_payout - sum(bets.values()))
                            db.session.commit()
                            if user_id in connected_users_sids:
                                socketio.emit('balance_update', 
                                            {'balance': user.balance},
                                            room=connected_users_sids[user_id])
                
                # 更新排行榜
                update_leaderboard()
                
                # 等待一段時間後開始新一局
                eventlet.sleep(5)
                
            except Exception as e:
                print(f"Error in game loop: {str(e)}")
                eventlet.sleep(1)
                continue

def update_leaderboard():
    """更新排行榜"""
    leaderboard = User.query.order_by(User.total_winnings.desc()).all()
    socketio.emit('leaderboard_update', [{
        'username': user.username,
        'total_winnings': user.total_winnings
    } for user in leaderboard])

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user and user.password == password:  # 實際應用中應該使用密碼雜湊
            login_user(user)
            return redirect(url_for('game'))
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if not User.query.filter_by(username=username).first():
            user = User(username=username, password=password)
            db.session.add(user)
            db.session.commit()
            return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/game')
@login_required
def game():
    return render_template('game.html')

@socketio.on('connect')
@login_required
def handle_connect():
    """處理用戶連接"""
    print(f"User {current_user.id} connected")
    connected_users_sids[current_user.id] = request.sid
    
    # 發送當前遊戲狀態
    emit('game_state', {
        'phase': game_state['phase'],
        'time_left': game_state['time_left'],
        'banker_current_score': game_state['banker_current_score'],
        'player_current_score': game_state['player_current_score']
    })
    
    # 發送用戶餘額
    emit('balance_update', {'balance': current_user.balance})
    
    # 更新排行榜
    update_leaderboard()

@socketio.on('disconnect')
@login_required
def handle_disconnect():
    # 用戶斷開連線時移除映射
    if current_user.id in connected_users_sids:
        del connected_users_sids[current_user.id]
        print(f"User {current_user.username} disconnected. SID: {request.sid}")

@socketio.on('place_bet')
@login_required
def handle_bet(data):
    """處理下注請求"""
    print(f"Received bet from user {current_user.id}: {data}")
    
    if game_state['phase'] != 'betting':
        emit('error', {'message': '現在不是下注時間'})
        return
    
    try:
        bet_type = data['type']
        amount = int(data['amount'])
        
        if amount <= 0:
            emit('error', {'message': '下注金額必須大於0'})
            return
            
        if current_user.balance < amount:
            emit('error', {'message': '餘額不足'})
            return
            
        # 更新用戶下注記錄
        if current_user.id not in game_state['bets']:
            game_state['bets'][current_user.id] = {}
        game_state['bets'][current_user.id][bet_type] = amount
        
        # 扣除下注金額
        current_user.balance -= amount
        db.session.commit()
        
        # 通知用戶餘額更新
        emit('balance_update', {'balance': current_user.balance})
        print(f"Bet placed successfully: {bet_type}, {amount}")
        
    except Exception as e:
        print(f"Error handling bet: {str(e)}")
        emit('error', {'message': '下注失敗，請稍後再試'})

def init_deck():
    """初始化牌組"""
    suits = ['♠', '♥', '♦', '♣']
    values = ['A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K']
    deck = []
    for _ in range(8):  # 8副牌
        for suit in suits:
            for value in values:
                # Add a rank attribute for display on the card front
                rank_for_display = value
                deck.append({'suit': suit, 'value': value, 'rank': rank_for_display})
    random.shuffle(deck)
    return deck

@app.route('/api/game/state')
def get_game_state():
    return jsonify(game_state)

if __name__ == '__main__':
    print("Starting the application...")
    try:
        eventlet.spawn(game_loop)
        socketio.run(app, debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
    except Exception as e:
        print(f"Error starting the application: {str(e)}") 