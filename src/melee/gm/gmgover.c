#include "gm_unsplit.h"
#include "gmevent.h"

#include "ft/forward.h"

#include "melee/gm/gm_1601.h"
#include "melee/gm/gm_16F1.h"
#include "melee/gm/gm_unsplit.h"
#include "melee/gm/types.h"
#include "melee/lb/lbmthp.h"

/* 1BEE9C */ static void gm_801BEE9C(GameModeState*);
/* 1BEF84 */ static void gm_801BEF84(GameModeState*);
/* 1BEFF0 */ static int gm_801BEFF0(void);
/* 1BF030 */ static int gm_801BF030(void);
/* 49C178 */ static u8 gm_8049C178[16];

typedef struct GameOverExitData {
    s8 next_game_mode;
    u8 pad_1[7];
} GameOverExitData;
ASSERT_SIZE(GameOverExitData, 0x8);

/* 4D6920 */ static GameOverExitData gm_804D6920;

GameModeState gm_Mode_GOver_States[] = {
    {
        0,
        lbDvdPreload_2,
        0,
        NULL,
        NULL,
        {
            GS_REGEND_TOYFALL,
            NULL,
            &gm_804D6920,
        },
    },
    {
        1,
        lbDvdPreload_2,
        0,
        NULL,
        NULL,
        {
            GS_STAFFROLL,
            NULL,
            NULL,
        },
    },
    {
        2,
        lbDvdPreload_2,
        0,
        NULL,
        gm_801BEF84,
        {
            GS_MOVIE_END,
            NULL,
            NULL,
        },
    },
    {
        3,
        lbDvdPreload_2,
        0,
        NULL,
        gm_801BEE9C,
        {
            GS_REGEND_CONGRATS,
            NULL,
            &gm_804D6920,
        },
    },
    { -1 },
};

GameModeState gm_Mode_DebugGOver_States[] = {
    {
        0,
        lbDvdPreload_2,
        0,
        NULL,
        NULL,
        {
            GS_REGEND_TOYFALL,
            NULL,
            &gm_804D6920,
        },
    },
    {
        1,
        lbDvdPreload_2,
        0,
        NULL,
        gm_801BEE9C,
        {
            GS_REGEND_CONGRATS,
            NULL,
            &gm_804D6920,
        },
    },
    { -1 },
};

void gm_801BEE9C(GameModeState* arg0)
{
    GameOverExitData* exit_data;
    u8 ckind;

    exit_data = arg0->info.exit_data;
    ckind = gm_80173224(gm_801BF030(), 1);
    if (gm_801BEFB0() == CKIND_GAMEWATCH && !gm_80164430(0x1B)) {
        gm_80164504(0x1B);
    }
    gm_8017390C(gm_801BF030(), 1);
    gm_80173EEC();
    gm_80172898(0x40);
    if (ckind == CHKIND_NONE) {
        if (!gm_80173754(1, gm_801BEFD0())) {
            gm_SetPendingGameMode(exit_data->next_game_mode);
        }
    } else {
        gm_801736E8(gm_801BEFB0(), gm_801BEFD0(), gm_801BF010(), gm_801BEFF0(),
                    ckind, exit_data->next_game_mode);
        gm_SetPendingGameMode(GM_CHALLENGER_APPROACH);
    }
    gm_SetNewGameModePending();
}

void gm_801BEF84(GameModeState* arg)
{
    lbMthp_8001F800();
}

void gm_801BEFA4(int ckind)
{
    gm_8049C178[0] = ckind;
}

CharacterKind gm_801BEFB0(void)
{
    return gm_8049C178[0];
}

void gm_801BEFC0(int arg0)
{
    gm_8049C178[1] = arg0;
}

int gm_801BEFD0(void)
{
    return gm_8049C178[1];
}

void gm_801BEFE0(s8 arg0)
{
    gm_8049C178[10] = arg0;
}

int gm_801BEFF0(void)
{
    return gm_8049C178[10];
}

void gm_801BF000(s8 arg0)
{
    gm_8049C178[9] = arg0;
}

int gm_801BF010(void)
{
    return gm_8049C178[9];
}

void gm_801BF020(s8 arg0)
{
    gm_8049C178[8] = arg0;
}

int gm_801BF030(void)
{
    return gm_8049C178[8];
}

void gm_801BF040(s8 arg0)
{
    gm_8049C178[2] = arg0;
}

int gm_801BF050(void)
{
    return gm_8049C178[2];
}
