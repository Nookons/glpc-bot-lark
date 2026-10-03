# Анализ реальных ошибок роботов

Источник: выгрузка из системы, 911 записей за 6 дней (2026-09-23 … 2026-09-28).
Файл данных: `data/sample_errors_2026-09-23_28.tsv` (14 колонок, TSV).

## Что это даёт

Это тот самый «сырой» материал, по которому нужно строить дерево. Он уже есть —
ждать 1–2 недели не нужно. Раньше дерево строилось по рисунку и оказалось
неверным: не тот состав объектов и не та глубина.

## Структура отчёта (5 уровней)

| Уровень | Что это | Размер меню |
| --- | --- | --- |
| 1. Объект | что сломалось | 6 |
| 2. Категория | чей это источник | 6 |
| 3. Тип проблемы | что именно не так | 15 |
| 4. Причина | из-за чего | 16 |
| 5. Решение | что сделали | 15 |

## Объекты

| Записей | Доля | Объект |
| ---: | ---: | --- |
| 559 | 61% | K50H |
| 241 | 26% | A42T-E2 hook |
| 73 | 8% | Work Station |
| 21 | 2% | Charging station |
| 14 | 1% | Material box |
| 3 | 0% | Tally station |

## Типы проблем

| Записей | Доля | Тип |
| ---: | ---: | --- |
| 500 | 54% | 行走异常Unable to drive |
| 119 | 13% | 料箱检测无容器No object detected |
| 100 | 10% | 取放货异常Abnormal pick-up and delivery |
| 33 | 3% | 速度错误Speed Error |
| 32 | 3% | 机器人安全装置触发Robot safety device triggered |
| 28 | 3% | 充电异常Abnormal charging |
| 22 | 2% | 机器人相互碰撞 Two robots collide |
| 21 | 2% | 网络通讯异常Network communication abnormality |
| 15 | 1% | 撞货架Collision with Shelf |
| 15 | 1% | 避障Obstacle avoidance |
| 9 | 0% | 卡箱异常Box stuck |
| 8 | 0% | 掉箱子或掉落件Drop Box or items |
| 4 | 0% | 电源模块异常Power module is abnormal |
| 3 | 0% | 区域限速故障 Zone speed limitation failure |
| 2 | 0% | 货叉检测无容器Forklift detection without container |

## Причины (подтипы)

| Записей | Доля | Причина |
| ---: | ---: | --- |
| 222 | 24% | 底盘相机故障 Chassis camera malfunction |
| 192 | 21% | 无法定义异常 Problem Cannot located |
| 119 | 13% | 举升高度误差Lifting height error |
| 85 | 9% | 取放箱位置错误 Wrong pick and place box position |
| 84 | 9% | 程序逻辑BUG Program logic bug |
| 52 | 5% | 举升机构异常 Abnormal lifting mechanism |
| 37 | 4% | 地面异物Foreign objects on the ground |
| 30 | 3% | 路径上有障碍物 Obstacle on the path |
| 26 | 2% | 地面码脏污 Ground code dirty |
| 21 | 2% | 安全模块故障Security module failure |
| 15 | 1% | 参数配置错误 Parameter configuration error |
| 13 | 1% | 硬件损坏 Hardware damage |
| 5 | 0% | 地脚定位偏差 Ground positioning deviation |
| 5 | 0% | 驱动组件异常 Driver component exception |
| 3 | 0% | 地面不平 Uneven ground |
| 2 | 0% | 地缝影响 Ground seam effect |

## Решения

| Записей | Доля | Решение |
| ---: | ---: | --- |
| 399 | 43% | Recovery |
| 209 | 22% | Recovery key and set to DM code |
| 119 | 13% | Case placed on robot, recovery |
| 63 | 6% | DM was cleaned, recovery key |
| 38 | 4% | Moved out |
| 25 | 2% | Change of position and recovery |
| 15 | 1% | Changed mode to Auto |
| 9 | 0% | Key |
| 9 | 0% | Remove the box |
| 8 | 0% | Changing CS |
| 7 | 0% | Remote recovery |
| 5 | 0% | Move closer |
| 2 | 0% | Restart |
| 2 | 0% | Put box back |
| 1 | 0% | Moved to charging station |

## Главные выводы

### 1. Категорию можно НЕ спрашивать — она выводится

Из 67 реальных комбинаций (объект, тип, причина) категория определяется
**однозначно во всех 67**. Значит сотрудник отвечает на 4 вопроса вместо 5,
а категория подставляется автоматически. Это минус один экран и минус ошибки.

### 2. Решение тоже почти всегда предсказуемо

По причине решение определяется однозначно в 7 из 16 случаев, то есть
автоматически подставить решение можно для 91% записей. Остальные —
показать первым самое частое и дать поправить.

### 3. Половина всех ошибок — одна ветка

`Unable to drive` — 500 из 911 (54%). Внутри 11 причин, из них главная
`Chassis camera malfunction` (222 записи, 24% от всего). Эту ветку надо
делать первой и тщательнее всего.

### 4. Каждый четвёртый раз причину не находят

`Problem Cannot located` — 192 записи (21%). По объектам по-разному:

| Объект | Не найдено | Доля |
| --- | ---: | ---: |
| K50H | 113/559 | 20% |
| A42T-E2 hook | 58/241 | 24% |
| Charging station | 10/21 | 47% |
| Material box | 8/14 | 57% |
| Tally station | 2/3 | 66% |
| Work Station | 1/73 | 1% |

У Material box и Tally station причина не определяется чаще, чем определяется.
Это не мусор: такие случаи надо либо разбирать детальнее, либо честно помечать.

### 5. Номера оборудования бывают составными

120 из 911 (13%) — два объекта сразу: `H108/1834`, `CS41/1759`, `237/1547`.
Поле ввода обязано принимать формат `A/B`, иначе такие ошибки не записать.

### 6. Списки сильно переиспользуются

Из 15 типов 11 встречаются у нескольких объектов. `Program logic bug`
встречается под 7 разными типами, `Problem Cannot located` — тоже под 7.
Значит уровни должны быть общими узлами по ссылке, а не копиями на каждую ветку.
Иначе дерево распухнет и правки разъедутся.

### 7. Дерево короче, чем кажется

67 уникальных полных путей. Покрытие:

* топ-1: 18%
* топ-3: 36%
* топ-5: 47%
* топ-10: 66%
* топ-20: 81%
* топ-30: 89%

То есть 20 путей покрывают 81% всех ошибок. Это не «бездонное» дерево —
основная масса сидит в узком наборе веток, а хвост длинный и редкий.

## Самые частые полные пути

| Раз | Объект | Тип | Причина |
| ---: | --- | --- | --- |
| 170 | K50H | 行走异常Unable to drive | 底盘相机故障 Chassis camera malfunction |
| 97 | K50H | 行走异常Unable to drive | 无法定义异常 Problem Cannot located |
| 64 | K50H | 料箱检测无容器No object detected | 举升高度误差Lifting height error |
| 52 | Work Station | 料箱检测无容器No object detected | 举升高度误差Lifting height error |
| 51 | A42T-E2 hook | 行走异常Unable to drive | 底盘相机故障 Chassis camera malfunction |
| 48 | A42T-E2 hook | 取放货异常Abnormal pick-up and delivery | 取放箱位置错误 Wrong pick and place box position |
| 37 | K50H | 行走异常Unable to drive | 地面异物Foreign objects on the ground |
| 31 | A42T-E2 hook | 行走异常Unable to drive | 无法定义异常 Problem Cannot located |
| 31 | A42T-E2 hook | 行走异常Unable to drive | 举升机构异常 Abnormal lifting mechanism |
| 24 | K50H | 速度错误Speed Error | 程序逻辑BUG Program logic bug |
| 24 | K50H | 行走异常Unable to drive | 地面码脏污 Ground code dirty |
| 22 | K50H | 行走异常Unable to drive | 路径上有障碍物 Obstacle on the path |
| 16 | K50H | 网络通讯异常Network communication abnormality | 程序逻辑BUG Program logic bug |
| 16 | A42T-E2 hook | 机器人相互碰撞 Two robots collide | 无法定义异常 Problem Cannot located |
| 13 | K50H | 机器人安全装置触发Robot safety device triggered | 安全模块故障Security module failure |

