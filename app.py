
import os

from flask import Flask, render_template, request, redirect, url_for, flash, send_file, jsonify, make_response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from io import BytesIO
from threading import Thread
import pandas as pd
import telebot
from dotenv import load_dotenv
from datetime import datetime, timedelta, date, time as dt_time
from apscheduler.schedulers.background import BackgroundScheduler
from pytz import timezone
from flask_migrate import Migrate   # добавляем импорт
from flask import send_from_directory
import calendar
from flask_login import current_user
from flask import abort
from calendar import monthrange
import pytz
from collections import Counter, defaultdict
import psutil
from dateutil.relativedelta import relativedelta
import numpy as np
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROUP_ID = int(os.getenv("TELEGRAM_GROUP_ID"))
GROUP_ID2 = int(os.getenv("TELEGRAM_GROUP_ID"))
GROUP_ID3 = int(os.getenv("TELEGRAM_GROUP_ID"))
# === Проверка токена ===
# === Инициализация Flask ===
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.db'  # Используем SQLite
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = '/var/www/db1/static/uploads'  # Полный путь для загрузки
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY")
app.config['SUPPLY_UPLOAD_FOLDER'] = os.path.join(app.config['UPLOAD_FOLDER'], '/var/www/db1/static/uploads/supplies')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # максимум 16 МБ
app.config['WTF_CSRF_TIME_LIMIT'] = None  # CSRF без истечения срока
app.config['SESSION_COOKIE_HTTPONLY'] = True

db = SQLAlchemy(app)
migrate = Migrate(app, db)  # <== вот это новое

login_manager = LoginManager(app)
login_manager.login_view = 'login'

# === Telegram Bot ===
bot = telebot.TeleBot(TOKEN)

#=== ПРОВЕРКА РОЛЕЙ ===
def require_admin():
    if current_user.role != 'admin':
        abort(403)

def require_worker_or_admin():
    if current_user.role not in ['admin', 'worker', 'pvz']:
        abort(403)




# === Модели ===
#== бд входа ===
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(150), nullable=False)
    role = db.Column(db.String(50), nullable=False, default='worker')  # 'admin' или 'worker'



#== бд ПВЗ ===
# ====== Модель ПВЗ ======
class Pvz(db.Model):
    __tablename__ = 'pvz'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    address = db.Column(db.String(255), nullable=True)
    # каскад для удобства: при удалении ПВЗ — удалить связанные сущности
    finances = db.relationship('PvzFinance', back_populates='pvz', cascade='all, delete-orphan', lazy=True)
    incomes = db.relationship('PvzIncomeDetail', back_populates='pvz', cascade='all, delete-orphan', lazy=True)
    staff = db.relationship('Staff', back_populates='pvz', cascade='all, delete-orphan', lazy=True)
    checkins = db.relationship('PvzCheckin', back_populates='pvz', cascade='all, delete-orphan', lazy=True)

    def __repr__(self):
        return f"<Pvz {self.name}>"



# ====== Финансы (по месяцу) ======
class PvzFinance(db.Model):
    __tablename__ = 'pvz_finance'
    id = db.Column(db.Integer, primary_key=True)
    pvz_id = db.Column(db.Integer, db.ForeignKey('pvz.id'), nullable=False)
    month = db.Column(db.String(20), nullable=False)  # "окт.-25"
    rent = db.Column(db.Float, nullable=True)
    workers = db.Column(db.Float, nullable=True)
    other = db.Column(db.Float, nullable=True)
    income = db.Column(db.Float, nullable=True)
    pvz = db.relationship('Pvz', back_populates='finances')
    income_details = db.relationship('PvzIncomeDetail', back_populates='finance', cascade='all, delete-orphan', lazy=True)

    def calc_taxes(self):
        return (self.income or 0) * 0.06

    def calc_total_expenses(self):
        return sum(filter(None, [self.rent or 0, self.workers or 0, self.other or 0, self.calc_taxes()]))

    def calc_profit(self):
        return (self.income or 0) - self.calc_total_expenses()



# ====== Доходы по источникам (привязка к ПВЗ) ======
class PvzIncomeDetail(db.Model):
    __tablename__ = 'pvz_income_detail'
    id = db.Column(db.Integer, primary_key=True)
    pvz_id = db.Column(db.Integer, db.ForeignKey('pvz.id'), nullable=False)
    finance_id = db.Column(db.Integer, db.ForeignKey('pvz_finance.id'), nullable=True)  # связь с конкретным месяцем
    source = db.Column(db.String(50), nullable=False)  # Яндекс, Авито и т.д.
    amount = db.Column(db.Float, nullable=False, default=0.0)
    pvz = db.relationship('Pvz', back_populates='incomes')
    finance = db.relationship('PvzFinance', back_populates='income_details')

    def __repr__(self):
        return f"<PvzIncomeDetail {self.source}: {self.amount}>"



# ====== Сотрудники ======
class Staff(db.Model):
    __tablename__ = 'staff'
    id = db.Column(db.Integer, primary_key=True)
    pvz_id = db.Column(db.Integer, db.ForeignKey('pvz.id'), nullable=False)
    name = db.Column(db.String(50), nullable=False)
    rate = db.Column(db.Float, default=0.0)  # ставка за смену
    pvz = db.relationship('Pvz', back_populates='staff')
    shifts = db.relationship('Shift', back_populates='worker', cascade='all, delete-orphan', lazy=True)
    checkins = db.relationship('PvzCheckin', back_populates='staff', lazy=True)



# ====== Смены ======
class Shift(db.Model):
    __tablename__ = 'shift'
    id = db.Column(db.Integer, primary_key=True)
    pvz_id = db.Column(db.Integer, db.ForeignKey('pvz.id'), nullable=False)  # добавлено
    date = db.Column(db.Date, nullable=False)
    worker_id = db.Column(db.Integer, db.ForeignKey('staff.id'), nullable=False)
    rate = db.Column(db.Float, default=0.0)
    worker = db.relationship('Staff', back_populates='shifts')
    pvz = db.relationship('Pvz')


# ====== Отметки сотрудников (checkin) ======
class PvzCheckin(db.Model):
    __tablename__ = 'pvz_checkin'
    id = db.Column(db.Integer, primary_key=True)
    pvz_id = db.Column(db.Integer, db.ForeignKey('pvz.id'), nullable=False)
    staff_id = db.Column(db.Integer, db.ForeignKey('staff.id'), nullable=False)
    staff_name = db.Column(db.String(50), nullable=False)
    timestamp = db.Column(db.DateTime, nullable=False)
    pvz = db.relationship('Pvz', back_populates='checkins')
    staff = db.relationship('Staff', back_populates='checkins')



#===== ошибки и заперты ====
@app.errorhandler(403)
def forbidden_error(error):
    return render_template('403.html'), 403

@app.errorhandler(404)
def forbidden_error(error):
    return render_template('404.html'), 404


#== авторизация
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


#== авторизация===
@app.route('/login', methods=['GET', 'POST'])

def login():
    error = None
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.password, password):
            login_user(user)

            if user.role == 'pvz':
                # Ищем сотрудника с именем похожим на логин
                staff = Staff.query.filter(Staff.name.ilike(f'%{username}%')).first()

                if staff:
                    return redirect(url_for('push_staff', pvz_id=staff.pvz_id))
                else:
                    # Если не нашли, пробуем по словарю
                    login_to_pvz = {
                        'work': 1,
                        'work2': 2,
                    }
                    pvz_id = login_to_pvz.get(username)
                    if pvz_id:
                        return redirect(url_for('push_staff', pvz_id=pvz_id))
                    else:
                        return redirect(url_for('select_pvz'))

            if user.role == 'worker':
                return redirect(url_for('push_staff'))

            if user.role == 'admin':
                return redirect(url_for('pvz_management'))

        error = 'Неверный логин или пароль'

    return render_template('login.html', error=error)

#== выход в авторизацию ===
@app.route('/logout')
@login_required
def logout():
    require_worker_or_admin()
    logout_user()
    return redirect(url_for('login'))


# ===== PVZ ФИНАНСЫ ======
# ===== PVZ ФИНАНСЫ ======

def update_total_income(finance_id):
    """Обновление общего дохода по источникам"""
    finance = PvzFinance.query.get(finance_id)
    if finance:
        total_income = sum(d.amount for d in finance.income_details)
        finance.income = total_income
        db.session.commit()


# Главная страница управления ПВЗ
@app.route('/')
@app.route('/pvz_management')
@login_required
def pvz_management():
    require_admin()
    pvz_list = Pvz.query.all()

    # Собираем статистику по каждому ПВЗ
    pvz_stats = []
    for pvz in pvz_list:
        # Количество сотрудников
        staff_count = Staff.query.filter_by(pvz_id=pvz.id).count()

        # Получаем все финансовые данные для ПВЗ
        finances = PvzFinance.query.filter_by(pvz_id=pvz.id).order_by(PvzFinance.id.desc()).all()
        months_count = len(finances)

        # Вычисляем общую прибыль (используем метод calc_profit())
        total_profit = 0
        total_income = 0  # Для расчета рентабельности

        for finance in finances:
            if finance.income:
                total_income += finance.income
                profit = finance.calc_profit()
                total_profit += profit

        # Прибыль за последний месяц (первый в списке, так как отсортировали по убыванию ID)
        last_month_profit = 0
        last_month_str = 'Нет данных'

        if finances:
            last_finance = finances[0]  # Самый свежий месяц
            last_month_profit = last_finance.calc_profit()
            last_month_str = last_finance.month

        # Процент рентабельности (прибыль / доход * 100)
        profit_percentage = None
        if total_income > 0 and total_profit > 0:
            profit_percentage = round((total_profit / total_income) * 100, 1)
        elif total_income > 0 and total_profit < 0:
            # Убыток
            profit_percentage = round((total_profit / total_income) * 100, 1)

        # Определяем период для отображения
        if months_count == 0:
            income_period = "Нет данных"
        elif months_count == 1:
            income_period = finances[0].month
        else:
            # Берем самый старый и самый свежий месяц
            oldest_finance = finances[-1]  # Последний в отсортированном списке
            newest_finance = finances[0]  # Первый в отсортированном списке
            income_period = f"{oldest_finance.month} - {newest_finance.month}"

        pvz_stats.append({
            'pvz': pvz,
            'staff_count': staff_count,
            'months_count': months_count,
            'last_month': last_month_str,
            'total_profit': total_profit,  # ОБЩАЯ ПРИБЫЛЬ
            'total_income': total_income,  # Общий доход для информации
            'last_month_profit': last_month_profit,  # Прибыль за последний месяц
            'profit_percentage': profit_percentage,  # Процент рентабельности
            'income_period': income_period,
        })

    return render_template('pvz_management.html', pvz_stats=pvz_stats, pvz_list=pvz_list)


# ====== Добавление ПВЗ ======
@app.route('/pvz/add_pvz', methods=['POST'])
@login_required
def add_pvz():
    require_admin()
    name = request.form['name']
    address = request.form.get('address', '')

    # Проверяем, нет ли уже ПВЗ с таким именем
    existing_pvz = Pvz.query.filter_by(name=name).first()
    if existing_pvz:
        flash(f"ПВЗ с именем '{name}' уже существует!")
        return redirect(url_for('pvz_management'))

    pvz = Pvz(name=name, address=address)
    db.session.add(pvz)
    db.session.commit()

    flash(f"✅ ПВЗ '{name}' успешно добавлен!")
    return redirect(url_for('pvz_management'))

# ====== Редактирование ПВЗ ======
@app.route('/pvz/edit_pvz/<int:pvz_id>', methods=['POST'])
@login_required
def edit_pvz_details(pvz_id):
    require_admin()
    pvz = Pvz.query.get_or_404(pvz_id)

    old_name = pvz.name
    pvz.name = request.form['name']
    pvz.address = request.form.get('address', '')

    db.session.commit()

    flash(f"✅ ПВЗ '{old_name}' успешно обновлен на '{pvz.name}'!")
    return redirect(url_for('pvz_management'))


# ====== Удаление ПВЗ ======
@app.route('/pvz/delete_full/<int:pvz_id>', methods=['POST'])
@login_required
def delete_pvz_full(pvz_id):
    require_admin()
    pvz = Pvz.query.get_or_404(pvz_id)

    # Сохраняем имя для сообщения
    pvz_name = pvz.name

    # Удаляем ПВЗ (каскадное удаление удалит все связанные записи)
    db.session.delete(pvz)
    db.session.commit()

    flash(f'✅ ПВЗ "{pvz_name}" и все связанные данные успешно удалены!', 'success')
    return redirect(url_for('pvz_management'))



@app.route('/pvz')
@login_required
def pvz():
    require_admin()

    # Получаем все ПВЗ для отображения в селекторе
    pvz_list = Pvz.query.all()

    # Получаем выбранный ПВЗ или первый по умолчанию
    selected_pvz_id = request.args.get('pvz_id')
    if selected_pvz_id:
        selected_pvz = Pvz.query.get(int(selected_pvz_id))
    elif pvz_list:
        selected_pvz = pvz_list[0]
    else:
        flash("Сначала добавьте ПВЗ")
        return redirect(url_for('index'))

    if not selected_pvz:
        flash("Сначала добавьте ПВЗ")
        return redirect(url_for('index'))

    # Получаем финансы для выбранного ПВЗ
    rows = PvzFinance.query.filter_by(pvz_id=selected_pvz.id).order_by(PvzFinance.id.asc()).all()

    data = []
    for r in rows:
        income_details = [{
            'id': i.id,
            'source': i.source,
            'amount': i.amount
        } for i in r.income_details]

        data.append({
            'id': r.id,
            'month': r.month,
            'rent': r.rent,
            'workers': r.workers,
            'other': r.other,
            'income': r.income,
            'taxes': r.calc_taxes() if r.income else None,
            'total_expenses': r.calc_total_expenses() if r.income else None,
            'profit': r.calc_profit() if r.income else None,
            'income_details': income_details
        })

    return render_template("pvz.html", data=data, pvz_list=pvz_list, selected_pvz=selected_pvz)





# ====== Добавление месяца ======
@app.route('/pvz/add', methods=['POST'])
@login_required
def add_pvz_month():
    require_admin()
    pvz_id = request.form['pvz_id']
    month = request.form['month']

    # Проверяем, нет ли уже записи за этот месяц
    existing_month = PvzFinance.query.filter_by(pvz_id=pvz_id, month=month).first()
    if existing_month:
        flash(f"❌ Запись за месяц '{month}' уже существует!")
        return redirect(url_for('pvz', pvz_id=pvz_id))

    rent = float(request.form.get('rent') or 0)
    workers = float(request.form.get('workers') or 0)
    other = float(request.form.get('other') or 0)
    income = float(request.form.get('income') or 0)

    finance = PvzFinance(
        pvz_id=pvz_id,
        month=month,
        rent=rent,
        workers=workers,
        other=other,
        income=income
    )
    db.session.add(finance)
    db.session.commit()

    flash(f"✅ Месяц '{month}' успешно добавлен!")

    return redirect(url_for('pvz', pvz_id=pvz_id))


# ====== Редактирование месяца ======
@app.route('/pvz/edit/<int:row_id>', methods=['POST'])
@login_required
def edit_pvz(row_id):
    require_admin()
    row = PvzFinance.query.get_or_404(row_id)

    old_income = row.income or 0
    row.rent = float(request.form.get('rent') or 0)
    row.workers = float(request.form.get('workers') or 0)
    row.other = float(request.form.get('other') or 0)
    row.income = float(request.form.get('income') or 0)

    db.session.commit()

    # Если изменился доход, пересчитываем налоги
    if row.income != old_income:
        update_total_income(row.id)

    flash(f"✅ Данные за месяц '{row.month}' успешно обновлены!")

    return redirect(url_for('pvz', pvz_id=row.pvz_id))


# ====== Удаление месяца ======
@app.route('/pvz/delete/<int:row_id>', methods=['POST'])
@login_required
def delete_pvz(row_id):
    require_admin()
    row = PvzFinance.query.get_or_404(row_id)
    pvz_id = row.pvz_id
    month_name = row.month

    db.session.delete(row)
    db.session.commit()

    flash(f"✅ Месяц '{month_name}' успешно удален!")

    return redirect(url_for('pvz', pvz_id=pvz_id))


# ====== ДОХОДЫ ПО ИСТОЧНИКАМ ======
@app.route('/pvz/income/add', methods=['POST'])
@login_required
def add_pvz_income():
    require_admin()
    pvz_id = request.form['pvz_id']
    finance_id = request.form['finance_id']
    source = request.form['source']
    amount = float(request.form.get('amount') or 0)

    if amount <= 0:
        flash("❌ Сумма дохода должна быть больше 0!")
        return redirect(url_for('pvz', pvz_id=pvz_id))

    income = PvzIncomeDetail(
        pvz_id=pvz_id,
        finance_id=finance_id,
        source=source,
        amount=amount
    )
    db.session.add(income)
    db.session.commit()
    update_total_income(finance_id)

    flash(f"✅ Доход '{source}' на сумму {amount:,.0f} ₽ успешно добавлен!")

    return redirect(url_for('pvz', pvz_id=pvz_id))


@app.route('/pvz/income/edit/<int:income_id>', methods=['POST'])
@login_required
def edit_pvz_income(income_id):
    require_admin()
    income = PvzIncomeDetail.query.get_or_404(income_id)

    old_source = income.source
    old_amount = income.amount

    income.source = request.form['source']
    income.amount = float(request.form.get('amount') or 0)

    if income.amount <= 0:
        flash("❌ Сумма дохода должна быть больше 0!")
        return redirect(url_for('pvz', pvz_id=income.pvz_id))

    db.session.commit()

    if income.finance_id:
        update_total_income(income.finance_id)

    flash(f"✅ Доход '{old_source}' ({old_amount:,.0f} ₽) обновлен на '{income.source}' ({income.amount:,.0f} ₽)!")

    return redirect(url_for('pvz', pvz_id=income.pvz_id))


@app.route('/pvz/income/delete/<int:income_id>', methods=['POST'])
@login_required
def delete_pvz_income(income_id):
    require_admin()
    income = PvzIncomeDetail.query.get_or_404(income_id)
    pvz_id = income.pvz_id
    finance_id = income.finance_id
    source_name = income.source
    amount = income.amount

    db.session.delete(income)
    db.session.commit()

    if finance_id:
        update_total_income(finance_id)

    flash(f"✅ Доход '{source_name}' на сумму {amount:,.0f} ₽ успешно удален!")

    return redirect(url_for('pvz', pvz_id=pvz_id))


# ====== Отправка отчёта в Telegram ======
@app.route('/pvz/notify', methods=['POST'])
@login_required
def notify_pvz():
    require_admin()
    pvz_id = request.form.get('pvz_id')
    selected_months = request.form.get('selected_months', '').strip()
    include_details = request.form.get('include_details', '1') == '1'

    # Получаем ПВЗ
    pvz = Pvz.query.get(pvz_id)
    if not pvz:
        flash("❌ ПВЗ не найден", "danger")
        return redirect(url_for('pvz', pvz_id=pvz_id))

    # Определяем, какие месяцы отправлять
    if selected_months:
        # Конвертируем строку с ID в список
        month_ids = []
        try:
            month_ids = [int(id.strip()) for id in selected_months.split(',') if id.strip()]
        except ValueError:
            flash("❌ Ошибка в формате выбранных месяцев", "danger")
            return redirect(url_for('pvz', pvz_id=pvz_id))

        # Получаем выбранные месяцы
        rows = PvzFinance.query.filter(
            PvzFinance.id.in_(month_ids),
            PvzFinance.pvz_id == pvz_id
        ).order_by(PvzFinance.id.desc()).all()

        if not rows:
            flash("❌ Не найдено данных для выбранных месяцев", "danger")
            return redirect(url_for('pvz', pvz_id=pvz_id))

        month_count_text = f"{len(rows)} выбранных месяцев"
    else:
        # По умолчанию - последний добавленный месяц
        rows = PvzFinance.query.filter_by(pvz_id=pvz_id) \
            .order_by(PvzFinance.id.desc()).first()
        rows = [rows] if rows else []

        if not rows:
            flash("❌ Нет данных для отправки", "danger")
            return redirect(url_for('pvz', pvz_id=pvz_id))

        month_count_text = "последний добавленный месяц"

    # Определяем, сколько месяцев отправляем
    month_count = len(rows)

    # Если детализация выключена И отправляется только один месяц
    if not include_details and month_count == 1:
        # Простой отчёт только для одного месяца
        r = rows[0]
        taxes = r.calc_taxes() if r.income else 0
        total_exp = r.calc_total_expenses() if r.income else 0
        profit = r.calc_profit() if r.income else 0

        # Эмодзи для прибыли/убытка
        profit_emoji = "📈" if profit >= 0 else "📉"

        # Формируем простое сообщение
        msg = f"📊 *ОТЧЕТ ПО ПВЗ: {pvz.name}*\n"
        if pvz.address:
            msg += f"📍 Адрес: {pvz.address}\n"
        msg += f"📅 {r.month}\n"
        msg += "─" * 20 + "\n\n"

        msg += f"💰 *Доход:* {r.income:,.0f} ₽\n"
        msg += f"💸 *Расходы:* {total_exp:,.0f} ₽\n"
        msg += f"🚀 *ПРИБЫЛЬ:* {profit_emoji} *{profit:,.0f} ₽*\n"  # Жирным

        # Добавляем время отправки
        from datetime import datetime
        msg += f"\n_📅 Отправлено: {datetime.now().strftime('%d.%m.%Y %H:%M')}_"

    else:
        # Полный отчёт с детализацией (много месяцев или включена детализация)
        msg = f"📊 *ОТЧЕТ ПО ПВЗ: {pvz.name}*\n"
        if pvz.address:
            msg += f"📍 Адрес: {pvz.address}\n"
        msg += f"📅 Отчёт за: {month_count_text}\n"
        msg += "─" * 30 + "\n\n"

        total_income = total_expenses = total_profit = 0

        for r in rows:
            taxes = r.calc_taxes() if r.income else 0
            total_exp = r.calc_total_expenses() if r.income else 0
            profit = r.calc_profit() if r.income else 0

            # Эмодзи для прибыли/убытка
            profit_emoji = "📈" if profit >= 0 else "📉"

            msg += f"*{r.month}:*\n"
            msg += f"💰 Доход: {r.income:,.0f} ₽\n"
            msg += f"💸 Расходы: {total_exp:,.0f} ₽\n"

            # Жирный шрифт для прибыли
            if month_count == 1 and not include_details:
                msg += f"🚀 Прибыль: {profit_emoji} *{profit:,.0f} ₽*\n"
            else:
                msg += f"📊 Прибыль: {profit_emoji} {profit:,.0f} ₽\n"

            # Добавляем детализацию доходов если есть и включена опция
            if include_details and r.income_details:
                msg += "📋 *Источники доходов:*\n"
                for inc in r.income_details:
                    percentage = (inc.amount / r.income * 100) if r.income > 0 else 0
                    msg += f"  • {inc.source}: {inc.amount:,.0f} ₽ ({percentage:.1f}%)\n"

            # Добавляем структуру расходов если включена детализация
            if include_details and total_exp > 0:
                msg += "📋 *Структура расходов:*\n"
                categories = [
                    ("Аренда", r.rent or 0),
                    ("Зарплаты", r.workers or 0),
                    ("Прочие", r.other or 0),
                    ("Налоги", taxes or 0)
                ]

                for category_name, amount in categories:
                    if amount > 0:
                        percentage = (amount / total_exp * 100) if total_exp > 0 else 0
                        msg += f"  • {category_name}: {amount:,.0f} ₽ ({percentage:.1f}%)\n"

            msg += "─" * 15 + "\n"

            total_income += r.income or 0
            total_expenses += total_exp
            total_profit += profit

        # Итоговая статистика для нескольких месяцев
        if month_count > 1:
            msg += f"\n*📊 ИТОГО ({month_count} месяцев):*\n"
            msg += f"💰 Общий доход: {total_income:,.0f} ₽\n"
            msg += f"💸 Общие расходы: {total_expenses:,.0f} ₽\n"
            msg += f"🚀 Общая прибыль: {total_profit:,.0f} ₽\n"

            # Добавляем рентабельность
            if total_income > 0:
                profitability = (total_profit / total_income) * 100
                profitability_emoji = "✅" if profitability > 0 else "⚠️"
                msg += f"📊 Рентабельность: {profitability_emoji} {profitability:.1f}%\n"

            # Добавляем средние показатели
            avg_income = total_income / month_count
            avg_profit = total_profit / month_count
            msg += f"\n*📈 Средние показатели за месяц:*\n"
            msg += f"💰 Средний доход: {avg_income:,.0f} ₽\n"
            msg += f"📊 Средняя прибыль: {avg_profit:,.0f} ₽\n"

        # Добавляем время отправки
        from datetime import datetime
        msg += f"\n_📅 Отправлено: {datetime.now().strftime('%d.%m.%Y %H:%M')}_"

    try:
        # Определяем, в какую группу отправлять отчёт
        if pvz.id == 2:  # Если отчёт для ПВЗ с ID=2
            if GROUP_ID3:
                bot.send_message(GROUP_ID3, msg, parse_mode='Markdown')
                target_group = "третью"
            else:
                # Если GROUP_ID3 не задан, отправляем в GROUP_ID2
                bot.send_message(GROUP_ID2, msg, parse_mode='Markdown')
                target_group = "вторую (третья не настроена)"
        else:
            bot.send_message(GROUP_ID2, msg, parse_mode='Markdown')
            target_group = "вторую"

        if not include_details and month_count == 1:
            flash(f"✅ Быстрый отчёт отправлен в {target_group} группу Telegram!", "success")
        else:
            flash(f"✅ Отчёт успешно отправлен в {target_group} группу Telegram! ({month_count} месяцев)", "success")
    except Exception as e:
        flash(f"❌ Ошибка при отправке отчета: {str(e)}", "danger")

    return redirect(url_for('pvz', pvz_id=pvz_id))


# Новый endpoint для быстрого отчёта (отдельный для кнопки "Текущий месяц")
@app.route('/pvz/notify_quick', methods=['POST'])
@login_required
def notify_quick():
    require_admin()
    try:
        data = request.get_json()
        pvz_id = data.get('pvz_id')
        month_ids = data.get('month_ids', [])

        if not pvz_id or not month_ids:
            return jsonify({'success': False, 'error': 'Не указаны параметры'}), 400

        pvz = Pvz.query.get(pvz_id)
        rows = PvzFinance.query.filter(
            PvzFinance.id.in_(month_ids),
            PvzFinance.pvz_id == pvz_id
        ).all()

        if not rows:
            return jsonify({'success': False, 'error': 'Нет данных для выбранных месяцев'}), 400

        # Формируем простое сообщение для одного месяца
        r = rows[0]
        taxes = r.calc_taxes() if r.income else 0
        total_exp = r.calc_total_expenses() if r.income else 0
        profit = r.calc_profit() if r.income else 0

        msg = f"🚀 *БЫСТРЫЙ ОТЧЕТ ПО ПВЗ: {pvz.name}*\n"
        msg += f"📅 {r.month}\n"
        msg += "─" * 20 + "\n\n"

        msg += f"💰 *Доход:* {r.income:,.0f} ₽\n"
        msg += f"💸 *Расходы:* {total_exp:,.0f} ₽\n"
        msg += f"🚀 *ПРИБЫЛЬ:* {'📈' if profit >= 0 else '📉'} *{profit:,.0f} ₽*\n\n"

        # В быстром отчёте всегда кратко
        if r.income_details:
            msg += "*📋 Основные источники доходов:*\n"
            # Показываем только топ-3 источника
            sorted_income = sorted(r.income_details, key=lambda x: x.amount, reverse=True)[:3]
            for inc in sorted_income:
                percentage = (inc.amount / r.income * 100) if r.income > 0 else 0
                msg += f"• {inc.source}: {inc.amount:,.0f} ₽ ({percentage:.1f}%)\n"

        # Добавляем время отправки
        from datetime import datetime
        msg += f"\n_📅 Отправлено: {datetime.now().strftime('%d.%m.%Y %H:%M')}_"

        try:
            # Определяем, в какую группу отправлять быстрый отчёт
            if pvz.id == 2:  # Если отчёт для ПВЗ с ID=2
                if GROUP_ID3:
                    bot.send_message(GROUP_ID3, msg, parse_mode='Markdown')
                    target_group = "третью"
                else:
                    bot.send_message(GROUP_ID2, msg, parse_mode='Markdown')
                    target_group = "вторую (третья не настроена)"
            else:
                bot.send_message(GROUP_ID2, msg, parse_mode='Markdown')
                target_group = "вторую"

            return jsonify({'success': True, 'target_group': target_group})

        except Exception as e:
            return jsonify({'success': False, 'error': f'Ошибка отправки в Telegram: {str(e)}'}), 500

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# 🧾 ПВЗ — Сотрудники (с поддержкой нескольких ПВЗ)
# ===============================
@app.route('/pvz/staff', methods=['GET', 'POST'])
@login_required
def pvz_staff():
    require_admin()

    # Получаем все ПВЗ для отображения в селекторе
    pvz_list = Pvz.query.all()

    # Получаем выбранный ПВЗ
    selected_pvz_id = request.args.get('pvz_id')
    if selected_pvz_id:
        selected_pvz = Pvz.query.get(int(selected_pvz_id))
    elif pvz_list:
        selected_pvz = pvz_list[0]
    else:
        flash("⚠️ Сначала добавьте ПВЗ")
        return redirect(url_for('pvz_management'))

    if not selected_pvz:
        flash("⚠️ Выберите ПВЗ")
        return redirect(url_for('pvz_management'))

    # --- Очистка старых данных для этого ПВЗ ---
    four_months_ago = date.today() - relativedelta(months=4)
    old_shifts = Shift.query.filter(
        Shift.pvz_id == selected_pvz.id,
        Shift.date < four_months_ago
    ).all()

    if old_shifts:
        for s in old_shifts:
            db.session.delete(s)
        db.session.commit()

    # --- Выбор месяца ---
    month_param = request.args.get('month')
    today = date.today()
    if month_param:
        year, month = map(int, month_param.split('-'))
        selected_date = date(year, month, 1)
    else:
        year, month = today.year, today.month
        selected_date = date(year, month, 1)

    # Получаем сотрудников выбранного ПВЗ
    staff_list = Staff.query.filter_by(pvz_id=selected_pvz.id).all()

    # Если нет сотрудников, показываем страницу с возможностью добавления
    if not staff_list:
        return render_template("pvz_staff.html",
                               staff_list=[],
                               stats=[],
                               shifts=[],
                               pvz_list=pvz_list,
                               selected_pvz=selected_pvz,
                               today=today.strftime('%d.%m.%Y'),
                               month_label=selected_date.strftime('%B %Y'),
                               current_worker="—",
                               next_salary_day=today.strftime('%d.%m.%Y'),
                               salary_to_pay=0,
                               salary_per_worker={},
                               worker_colors={},
                               transition_in_progress=False)

    # --- Цвета для работников ---
    worker_colors = {}
    colors_list = ['#f8d7da33', '#d1ecf133', '#fff3cd33', '#d4edda33']
    for i, w in enumerate(staff_list):
        worker_colors[w.id] = colors_list[i % len(colors_list)]

    # --- Загрузка смен (включая 20 дней прошлого месяца) ---
    days_in_month = monthrange(year, month)[1]
    start_date = date(year, month, 1) - timedelta(days=20)
    end_date = date(year, month, days_in_month)

    existing_shifts = Shift.query.filter(
        Shift.pvz_id == selected_pvz.id,
        Shift.date.between(start_date, end_date)
    ).order_by(Shift.date.asc()).all()

    # Если сотрудников меньше 2, не создаем график автоматически
    if len(staff_list) >= 2:
        # --- Определяем, с кого начать месяц ---
        prev_month_end = date(year, month, 1) - timedelta(days=1)
        prev_month_start = prev_month_end - timedelta(days=20)

        last_shifts = Shift.query.filter(
            Shift.pvz_id == selected_pvz.id,
            Shift.date.between(prev_month_start, prev_month_end)
        ).order_by(Shift.date.desc()).all()

        # Определяем последнего работавшего сотрудника
        last_worker_id = None
        consecutive_days = 0
        start_block_day = 0

        if last_shifts:
            current_date = prev_month_end
            days_checked = 0

            while current_date >= prev_month_start and days_checked < 10:
                shift_on_date = next((s for s in last_shifts if s.date == current_date), None)

                if shift_on_date:
                    if last_worker_id is None:
                        last_worker_id = shift_on_date.worker_id
                        consecutive_days = 1
                    elif shift_on_date.worker_id == last_worker_id:
                        consecutive_days += 1
                    else:
                        break
                else:
                    break

                current_date -= timedelta(days=1)
                days_checked += 1

        # Определяем, с кого начинать
        staff_count = len(staff_list)
        if staff_count >= 2:
            if last_worker_id:
                for i, staff in enumerate(staff_list):
                    if staff.id == last_worker_id:
                        last_worker_index = i
                        break

                if consecutive_days >= 2:
                    start_worker_index = (last_worker_index + 1) % staff_count
                    start_block_day = 0
                else:
                    start_worker_index = last_worker_index
                    start_block_day = consecutive_days
            else:
                start_worker_index = 0
                start_block_day = 0
        else:
            start_worker_index = 0
            start_block_day = 0

        # --- Создание графика 2/2 ---
        shifts = []
        current_worker_index = start_worker_index
        days_in_current_block = start_block_day

        for i in range(days_in_month):
            d = date(year, month, i + 1)
            shift_day = next((s for s in existing_shifts if s.date == d), None)

            if shift_day:
                shifts.append(shift_day)
                for idx, staff in enumerate(staff_list):
                    if staff.id == shift_day.worker_id:
                        current_worker_index = idx
                        if i > 0:
                            prev_date = date(year, month, i)
                            prev_shift = next((s for s in existing_shifts if s.date == prev_date), None)
                            if prev_shift and prev_shift.worker_id == shift_day.worker_id:
                                days_in_current_block = 1
                            else:
                                days_in_current_block = 0
                        else:
                            days_in_current_block = 0
                        break
            else:
                if days_in_current_block >= 2:
                    current_worker_index = (current_worker_index + 1) % staff_count
                    days_in_current_block = 0

                worker = staff_list[current_worker_index]
                shift_day = Shift(
                    pvz_id=selected_pvz.id,
                    date=d,
                    worker_id=worker.id,
                    rate=worker.rate
                )
                db.session.add(shift_day)
                db.session.commit()
                shifts.append(shift_day)
                days_in_current_block += 1
    else:
        # Если меньше 2 сотрудников, просто показываем существующие смены
        shifts = [s for s in existing_shifts if s.date.year == year and s.date.month == month]
        shifts.sort(key=lambda x: x.date)

    # --- Текущая смена ---
    if year == today.year and month == today.month:
        current_shift = next((s for s in shifts if s.date == today), None)
        current_worker = current_shift.worker.name if current_shift else "—"
    else:
        current_worker = None

    # --- Расчет зарплаты ---
    today_day = today.day
    month_for_salary = today.month
    year_for_salary = today.year

    if today_day <= 10:
        prev_month = month_for_salary - 1 if month_for_salary > 1 else 12
        prev_year = year_for_salary if month_for_salary > 1 else year_for_salary - 1
        last_salary_date = date(prev_year, prev_month, 25)
        next_salary_day = date(year_for_salary, month_for_salary, 10)
    elif today_day <= 25:
        last_salary_date = date(year_for_salary, month_for_salary, 10)
        next_salary_day = date(year_for_salary, month_for_salary, 25)
    else:
        last_salary_date = date(year_for_salary, month_for_salary, 25)
        next_month = month_for_salary + 1 if month_for_salary < 12 else 1
        next_year = year_for_salary + 1 if next_month == 1 else year_for_salary
        next_salary_day = date(next_year, next_month, 10)

    # --- Подсчёт выплат ---
    salary_per_worker = {}
    for s in staff_list:
        # Берем смены за период выплат
        worker_shifts = [sh for sh in existing_shifts
                         if sh.worker_id == s.id
                         and last_salary_date < sh.date <= next_salary_day]
        salary_per_worker[s.name] = sum(sh.rate for sh in worker_shifts)

    salary_to_pay = sum(salary_per_worker.values())
    month_label = selected_date.strftime('%B %Y')

    # Флаг для отображения уведомления о переходе
    transition_in_progress = today >= date(2025, 11, 21) and today < date(2026, 1, 1)

    # Добавляем информацию о времени прихода к сменам
    for shift in shifts:
        checkin = PvzCheckin.query.filter(
            PvzCheckin.staff_id == shift.worker_id,
            db.func.date(PvzCheckin.timestamp) == shift.date,
            PvzCheckin.pvz_id == selected_pvz.id
        ).order_by(PvzCheckin.timestamp.asc()).first()

        shift.checkin_time = checkin.timestamp if checkin else None

    # --- Статистика ---
    stats = []
    for s in staff_list:
        shifts_done = sum(1 for sh in shifts if sh.worker_id == s.id and sh.date <= today)
        shifts_expected = sum(1 for sh in shifts if sh.worker_id == s.id)
        salary_done = sum(sh.rate for sh in shifts if sh.worker_id == s.id and sh.date <= today)
        salary_expected = sum(sh.rate for sh in shifts if sh.worker_id == s.id)

        stats.append({
            'id': s.id,
            'name': s.name,
            'shifts_done': shifts_done,
            'shifts_expected': shifts_expected,
            'salary_done': salary_done,
            'salary_expected': salary_expected
        })

    return render_template("pvz_staff.html",
                           staff_list=staff_list,
                           stats=stats,
                           shifts=shifts,
                           current_worker=current_worker,
                           pvz_list=pvz_list,
                           selected_pvz=selected_pvz,
                           today=today.strftime('%d.%m.%Y'),
                           month_label=month_label,
                           next_salary_day=next_salary_day.strftime('%d.%m.%Y'),
                           salary_to_pay=salary_to_pay,
                           salary_per_worker=salary_per_worker,
                           worker_colors=worker_colors,
                           transition_in_progress=transition_in_progress)


# ➕ Добавление сотрудника (с привязкой к ПВЗ)
@app.route('/pvz/staff/add', methods=['POST'])
@login_required
def add_staff():
    require_admin()
    pvz_id = request.form['pvz_id']
    name = request.form['name'].strip()
    rate = float(request.form.get('rate') or 0)

    # Проверяем, нет ли уже сотрудника с таким именем в этом ПВЗ
    existing_staff = Staff.query.filter_by(pvz_id=pvz_id, name=name).first()
    if existing_staff:
        flash(f"❌ Сотрудник с именем '{name}' уже существует в этом ПВЗ!")
        return redirect(url_for('pvz_staff', pvz_id=pvz_id))

    if rate < 0:
        flash("❌ Ставка не может быть отрицательной!")
        return redirect(url_for('pvz_staff', pvz_id=pvz_id))

    staff = Staff(pvz_id=pvz_id, name=name, rate=rate)
    db.session.add(staff)
    db.session.commit()

    flash(f"✅ Сотрудник '{name}' успешно добавлен!")
    return redirect(url_for('pvz_staff', pvz_id=pvz_id))


# ✏️ Изменение имени и ставки сотрудника
@app.route('/pvz/staff/edit/<int:staff_id>', methods=['POST'])
@login_required
def edit_staff(staff_id):
    require_admin()
    staff = Staff.query.get_or_404(staff_id)

    old_name = staff.name
    old_rate = staff.rate

    new_name = request.form['name'].strip()
    new_rate = float(request.form.get('rate') or 0)

    # Проверяем, что новое имя уникальное (если оно изменилось)
    if new_name != old_name:
        existing_staff = Staff.query.filter_by(pvz_id=staff.pvz_id, name=new_name).first()
        if existing_staff:
            flash(f"❌ Сотрудник с именем '{new_name}' уже существует в этом ПВЗ!")
            return redirect(url_for('pvz_staff', pvz_id=staff.pvz_id))

    if new_rate < 0:
        flash("❌ Ставка не может быть отрицательной!")
        return redirect(url_for('pvz_staff', pvz_id=staff.pvz_id))

    staff.name = new_name
    staff.rate = new_rate
    db.session.commit()

    flash(f"✅ Сотрудник '{old_name}' обновлен! Новое имя: '{new_name}', ставка: {new_rate:,.0f} ₽")
    return redirect(url_for('pvz_staff', pvz_id=staff.pvz_id))


# ✏️ Изменение смены (без времени прихода)
@app.route('/pvz/staff/change_shift', methods=['POST'])
@login_required
def change_shift():
    require_admin()
    shift_id = int(request.form['shift_id'])
    worker_id = int(request.form['worker_id'])
    rate = float(request.form.get('rate') or 0)

    shift = Shift.query.get_or_404(shift_id)

    old_worker = shift.worker.name if shift.worker else "Неизвестно"
    new_worker = Staff.query.get(worker_id)

    if not new_worker:
        flash("❌ Указанный сотрудник не найден!")
        return redirect(url_for('pvz_staff', pvz_id=shift.pvz_id))

    if rate < 0:
        flash("❌ Ставка не может быть отрицательной!")
        return redirect(url_for('pvz_staff', pvz_id=shift.pvz_id))

    shift.worker_id = worker_id
    shift.rate = rate
    db.session.commit()

    flash(f"✅ Смена на {shift.date.strftime('%d.%m.%Y')} изменена: {old_worker} → {new_worker.name}")
    return redirect(url_for('pvz_staff', pvz_id=shift.pvz_id))


# 🗑 Удаление сотрудника (без проверки на минимум 2 сотрудника)
@app.route('/pvz/staff/delete/<int:staff_id>', methods=['POST'])
@login_required
def delete_staff(staff_id):
    require_admin()
    staff = Staff.query.get_or_404(staff_id)
    pvz_id = staff.pvz_id
    staff_name = staff.name

    try:
        # 1-й способ: Удалить все связанные смены
        Shift.query.filter_by(worker_id=staff_id).delete()

        # 2-й способ: Удалить все отметки прихода
        PvzCheckin.query.filter_by(staff_id=staff_id).delete()

        # Теперь удаляем сотрудника
        db.session.delete(staff)
        db.session.commit()

        flash(f"✅ Сотрудник '{staff_name}' успешно удален!")

    except Exception as e:
        db.session.rollback()
        flash(f"❌ Ошибка при удалении сотрудника: {str(e)}", "danger")

    return redirect(url_for('pvz_staff', pvz_id=pvz_id))


# 👤 Страница Push Staff (с выбором ПВЗ)
@app.route('/pvz/push_staff', methods=['GET', 'POST'])
@login_required
def push_staff():
    require_worker_or_admin()

    moscow_tz = pytz.timezone('Europe/Moscow')
    now = datetime.now(moscow_tz)
    time_str = now.strftime('%H:%M:%S')

    # Получаем все ПВЗ для выбора
    pvz_list = Pvz.query.all()

    # Получаем выбранный ПВЗ
    selected_pvz_id = request.args.get('pvz_id')
    if selected_pvz_id:
        selected_pvz = Pvz.query.get(int(selected_pvz_id))
    elif pvz_list:
        selected_pvz = pvz_list[0]
    else:
        flash("⚠️ Сначала добавьте ПВЗ")
        return redirect(url_for('pvz_management'))

    if request.method == 'POST':
        staff_id = int(request.form['staff_id'])
        pvz_id = request.form.get('pvz_id', selected_pvz.id)

        staff = Staff.query.get_or_404(staff_id)

        # Проверяем, что сотрудник принадлежит выбранному ПВЗ
        if staff.pvz_id != int(pvz_id):
            flash("❌ Ошибка: сотрудник не принадлежит выбранному ПВЗ")
            return redirect(url_for('push_staff', pvz_id=pvz_id))

        # === Сохраняем отметку в БД ===
        checkin = PvzCheckin(
            pvz_id=pvz_id,
            staff_id=staff.id,
            staff_name=staff.name,
            timestamp=now
        )
        db.session.add(checkin)
        db.session.commit()

        # === Отправляем в Telegram ===
        pvz_name = selected_pvz.name
        msg = f"👤 *НАЧАЛО РАБОТЫ*\n\n"
        msg += f"🏪 ПВЗ: {pvz_name}\n"
        msg += f"👷 Сотрудник: {staff.name}\n"
        msg += f"🕐 Время: {time_str} (МСК)\n"
        msg += f"📅 Дата: {now.strftime('%d.%m.%Y')}"

        try:
            # ВАРИАНТ 1: Если сотрудник из ПВЗ с ID=2 - отправляем в третью группу
            if staff.pvz_id == 2:  # Проверка по ID ПВЗ
                bot.send_message(GROUP_ID3, msg, parse_mode='Markdown')
                target_group = "третью"
            else:
                bot.send_message(GROUP_ID2, msg, parse_mode='Markdown')
                target_group = "вторую"

            flash(f"✅ Сотрудник {staff.name} начал работу. Уведомление отправлено в {target_group} группу Telegram!")
        except Exception as e:
            flash(f"⚠️ Отметка сохранена, но не удалось отправить в Telegram: {str(e)}")

        return redirect(url_for('push_staff', pvz_id=pvz_id))

    # Получаем сотрудников выбранного ПВЗ
    staff_list = Staff.query.filter_by(pvz_id=selected_pvz.id).all()

    return render_template('push_staff.html',
                           staff_list=staff_list,
                           pvz_list=pvz_list,
                           selected_pvz=selected_pvz,
                           time_str=time_str)


# ЭКСЕЛЬ ЗП (с фильтром по ПВЗ)
@app.route('/download_salary_excel')
@login_required
def download_salary_excel():
    require_admin()

    # Получаем параметр ПВЗ
    pvz_id = request.args.get('pvz_id')

    # --- Определяем период выплат ---
    today = date.today()
    today_day = today.day
    year_for_salary = today.year
    month_for_salary = today.month

    if today_day <= 10:
        prev_month = month_for_salary - 1 if month_for_salary > 1 else 12
        prev_year = year_for_salary if month_for_salary > 1 else year_for_salary - 1
        last_salary_date = date(prev_year, prev_month, 25)
        next_salary_day = date(year_for_salary, month_for_salary, 10)
    elif today_day <= 25:
        last_salary_date = date(year_for_salary, month_for_salary, 10)
        next_salary_day = date(year_for_salary, month_for_salary, 25)
    else:
        last_salary_date = date(year_for_salary, month_for_salary, 25)
        next_month = month_for_salary + 1 if month_for_salary < 12 else 1
        next_year = year_for_salary + 1 if next_month == 1 else year_for_salary
        next_salary_day = date(next_year, next_month, 10)

    # --- Формируем запрос с учетом ПВЗ ---
    shifts_query = Shift.query.filter(
        Shift.date > last_salary_date,
        Shift.date <= next_salary_day
    )

    if pvz_id:
        shifts_query = shifts_query.filter_by(pvz_id=pvz_id)
        pvz = Pvz.query.get(pvz_id)
        pvz_name = pvz.name if pvz else ""

    shifts = shifts_query.order_by(Shift.date.asc()).all()

    # --- Формируем таблицу для Excel ---
    rows = []

    # Заголовок с именем ПВЗ
    if pvz_id:
        rows.append([f"ПВЗ: {pvz_name}"])
    else:
        rows.append(["Общий отчет по всем ПВЗ"])

    rows.append([f"Период выплат: с {last_salary_date.strftime('%d.%m.%Y')} по {next_salary_day.strftime('%d.%m.%Y')}"])
    rows.append(["Дата формирования:", datetime.now().strftime('%d.%m.%Y %H:%M')])
    rows.append([])

    # Получаем сотрудников с фильтром по ПВЗ
    staff_query = Staff.query
    if pvz_id:
        staff_query = staff_query.filter_by(pvz_id=pvz_id)

    staff_list = staff_query.all()

    if not staff_list:
        rows.append(["Нет сотрудников в выбранном ПВЗ"])
    else:
        total_all_salary = 0

        for s in staff_list:
            worker_shifts = [sh for sh in shifts if sh.worker_id == s.id]
            if worker_shifts:
                rows.append([f"Сотрудник: {s.name}"])
                rows.append(["Дата смены", "Ставка за смену (₽)"])
                for sh in worker_shifts:
                    rows.append([sh.date.strftime("%d.%m.%Y"), f"{sh.rate:,.0f}"])
                total_salary = sum(sh.rate for sh in worker_shifts)
                total_all_salary += total_salary
                rows.append(["Итого к выплате:", f"{total_salary:,.0f} ₽"])
                rows.append([])

        # Итоговая строка
        if pvz_id:
            rows.append([f"Общая сумма к выплате по ПВЗ '{pvz_name}':", f"{total_all_salary:,.0f} ₽"])
        else:
            rows.append(["Общая сумма к выплате по всем ПВЗ:", f"{total_all_salary:,.0f} ₽"])

    # --- Создаём Excel ---
    if not rows or len(rows) <= 5:
        rows.append(["Нет данных за выбранный период"])

    df = pd.DataFrame(rows)
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, header=False, sheet_name='Выплаты')

        # Форматируем ширину колонок
        worksheet = writer.sheets['Выплаты']
        for col in worksheet.columns:
            max_length = 0
            column = col[0].column_letter
            for cell in col:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except:
                    pass
            adjusted_width = min(max_length + 2, 50)
            worksheet.column_dimensions[column].width = adjusted_width

    output.seek(0)

    if pvz_id and pvz_name:
        filename = f"Выплаты_ПВЗ_{pvz_name}_{last_salary_date.strftime('%d.%m')}_{next_salary_day.strftime('%d.%m.%Y')}.xlsx"
    else:
        filename = f"Выплаты_все_ПВЗ_{last_salary_date.strftime('%d.%m')}_{next_salary_day.strftime('%d.%m.%Y')}.xlsx"

    return send_file(output, as_attachment=True, download_name=filename,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ====== БЫСТРОЕ ДОБАВЛЕНИЕ СОТРУДНИКА (AJAX) ======
@app.route('/pvz/staff/add_ajax', methods=['POST'])
@login_required
def add_staff_ajax():
    require_admin()
    try:
        data = request.get_json()
        pvz_id = data.get('pvz_id')
        name = data.get('name', '').strip()
        rate = float(data.get('rate', 0))

        if not name:
            return jsonify({'success': False, 'message': 'Имя сотрудника обязательно'})

        # Проверяем, нет ли уже сотрудника с таким именем в этом ПВЗ
        existing_staff = Staff.query.filter_by(pvz_id=pvz_id, name=name).first()
        if existing_staff:
            return jsonify({'success': False, 'message': f'Сотрудник с именем "{name}" уже существует'})

        if rate < 0:
            return jsonify({'success': False, 'message': 'Ставка не может быть отрицательной'})

        staff = Staff(pvz_id=pvz_id, name=name, rate=rate)
        db.session.add(staff)
        db.session.commit()

        return jsonify({
            'success': True,
            'message': f'Сотрудник "{name}" добавлен',
            'staff': {
                'id': staff.id,
                'name': staff.name,
                'rate': staff.rate
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'message': f'Ошибка: {str(e)}'})


# ====== ПОЛУЧЕНИЕ СОТРУДНИКОВ ПВЗ (AJAX) ======
@app.route('/pvz/staff/get_by_pvz/<int:pvz_id>', methods=['GET'])
@login_required
def get_staff_by_pvz(pvz_id):
    require_admin()
    staff_list = Staff.query.filter_by(pvz_id=pvz_id).all()

    staff_data = []
    for staff in staff_list:
        staff_data.append({
            'id': staff.id,
            'name': staff.name,
            'rate': staff.rate
        })

    return jsonify({'success': True, 'staff': staff_data})



# === Запуск бота и сервера ===
def run_bot():
    bot.polling(none_stop=True)




if __name__ == '__main__':
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    with app.app_context():
        db.create_all()
    Thread(target=run_bot).start()
    app.run( debug=False)